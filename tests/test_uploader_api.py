import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth
import db
import discord_live
import editor_store as es
import gdrive
import scheduler
import uploader_api
from config import config

SECRET = {"X-Agent-Secret": "s3cret"}
JOB = "0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def drive(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    scheduler._reset_for_tests()
    monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "s3cret"})
    db.meta_set("gdrive_projects", "PROJ")
    s = {"quota": {"limit": 10**12, "usage": 0}, "folders": 0, "sessions": [], "fail_start": 0, "files": {}}

    async def about():
        return s["quota"]

    async def create_folder(name, parent):
        s["folders"] += 1
        return f"F{s['folders']}"

    async def start(name, parent, size, mime, props, origin):
        if s["fail_start"]:
            s["fail_start"] -= 1
            raise gdrive.DriveError("boom")
        s["sessions"].append({"name": name, "parent": parent, "size": size, "mime": mime, "props": props,
                              "origin": origin})
        return f"https://upload/{len(s['sessions'])}"

    async def get_file(fid):
        return s["files"].get(fid, {})

    async def share(*a, **k):
        pass

    for name, fn in (("about", about), ("create_folder", create_folder), ("start_resumable_upload", start),
                     ("get_file", get_file), ("share_with", share)):
        monkeypatch.setattr(gdrive, name, fn)
    return s


@contextlib.asynccontextmanager
async def api():
    app = web.Application(middlewares=[auth.auth_middleware])
    uploader_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async def call(method, path, json=None, headers=SECRET):
            resp = await client.request(method, path, json=json, headers=headers)
            return resp.status, await resp.json()
        yield call
    finally:
        await client.close()


def _body(**kw):
    return {"job_id": JOB, "filename": "2026-10-27 18-00-00.mp4", "size": 1000, "mime": "video/mp4",
            "duration_s": 3600, "stream_date": "2026-10-27", **kw}


def _projects():
    return es._c().execute("SELECT COUNT(*) FROM projects").fetchone()[0]


@pytest.mark.asyncio
async def test_every_route_refuses_without_secret():
    es.create_project("A", "2026-10-27", "", "a.mp4", 5)
    async with api() as call:
        for method, path, body in (("POST", "/api/uploader/projects", _body()),
                                   ("POST", "/api/uploader/projects/1/complete", {"file_id": "X"}),
                                   ("GET", "/api/uploader/live", None)):
            for headers in ({}, {"X-Agent-Secret": "wrong"}):
                status, resp = await call(method, path, body, headers)
                assert (status, resp) == (401, {"error": "Invalid or missing agent secret"}), (path, headers)
    assert _projects() == 1


@pytest.mark.asyncio
async def test_refuses_when_secret_unset(monkeypatch):
    monkeypatch.setitem(config._data, "ocr_agent_secret", "")
    async with api() as call:
        assert (await call("GET", "/api/uploader/live", headers={"X-Agent-Secret": ""}))[0] == 401
        assert (await call("POST", "/api/uploader/projects", _body(), headers={"X-Agent-Secret": ""}))[0] == 401


@pytest.mark.asyncio
async def test_create_makes_project_and_session(drive, monkeypatch):
    monkeypatch.setattr(discord_live, "last_live_title", lambda d: "Ranked grind" if d == "2026-10-27" else "")
    async with api() as call:
        status, resp = await call("POST", "/api/uploader/projects", _body(filename="C:\\x\\..\\vod.mkv", mime=""))
    assert status == 200
    p = es.get_project(resp["project_id"])
    assert resp == {"project_id": p["id"], "kind": "vod", "status": "uploading", "upload_url": "https://upload/1"}
    assert (p["name"], p["stream_title"], p["vod_filename"], p["vod_size"]) == ("vod", "Ranked grind", "vod.mkv", 1000)
    assert p["vod_upload_url"] == "https://upload/1"
    assert drive["sessions"] == [{"name": "vod.mkv", "parent": p["drive_folder_id"], "size": 1000,
                                  "mime": "video/mp4", "props": {"project_id": str(p["id"])}, "origin": None}]
    assert db.meta_get(f"uploader_job_{JOB}") == str(p["id"])


@pytest.mark.asyncio
async def test_create_title_falls_back_to_stem():
    async with api() as call:
        _, resp = await call("POST", "/api/uploader/projects", _body())
    assert es.get_project(resp["project_id"])["stream_title"] == "2026-10-27 18-00-00"


@pytest.mark.asyncio
async def test_create_short_file_is_clip_with_reel():
    async with api() as call:
        status, resp = await call("POST", "/api/uploader/projects", _body(duration_s=42.5))
    assert status == 200 and resp["kind"] == "clip"
    assert es.get_project(resp["project_id"])["reel_tape_id"] is not None


@pytest.mark.asyncio
async def test_create_is_idempotent_by_job_id(drive):
    async with api() as call:
        _, a = await call("POST", "/api/uploader/projects", _body())
        _, b = await call("POST", "/api/uploader/projects", _body())
    assert a == b
    assert _projects() == 1 and len(drive["sessions"]) == 1


@pytest.mark.asyncio
async def test_create_retries_session_after_drive_failure(drive):
    drive["fail_start"] = 1
    async with api() as call:
        status, _ = await call("POST", "/api/uploader/projects", _body())
        assert status == 502
        assert _projects() == 1
        status, resp = await call("POST", "/api/uploader/projects", _body())
    assert status == 200 and resp["upload_url"] == "https://upload/1"
    assert _projects() == 1
    assert es.get_project(resp["project_id"])["vod_upload_url"] == "https://upload/1"


@pytest.mark.asyncio
async def test_new_session_replaces_url(drive):
    async with api() as call:
        _, a = await call("POST", "/api/uploader/projects", _body())
        _, b = await call("POST", "/api/uploader/projects", _body(new_session=True))
    assert (a["upload_url"], b["upload_url"]) == ("https://upload/1", "https://upload/2")
    assert es.get_project(b["project_id"])["vod_upload_url"] == "https://upload/2"


@pytest.mark.asyncio
async def test_ready_project_answers_ready_without_new_session(drive):
    async with api() as call:
        _, a = await call("POST", "/api/uploader/projects", _body())
        es.mark_vod_ready(a["project_id"], "VOD")
        status, b = await call("POST", "/api/uploader/projects", _body(new_session=True))
    assert status == 200
    assert b == {"project_id": a["project_id"], "kind": "vod", "status": "ready", "upload_url": ""}
    assert len(drive["sessions"]) == 1


@pytest.mark.asyncio
async def test_deleted_project_answers_409(drive):
    async with api() as call:
        _, a = await call("POST", "/api/uploader/projects", _body())
        for status in ("deleting", "deleted"):
            db.get().execute("UPDATE projects SET vod_status=? WHERE id=?", (status, a["project_id"]))
            assert await call("POST", "/api/uploader/projects", _body(new_session=True)) == (
                409, {"error": "This VOD was deleted"})
            assert await call("POST", f"/api/uploader/projects/{a['project_id']}/complete", {"file_id": "VOD"}) == (
                409, {"error": "This VOD was deleted"})
    assert len(drive["sessions"]) == 1


@pytest.mark.asyncio
async def test_create_refuses_when_drive_full(drive):
    drive["quota"] = {"limit": 500, "usage": 0}
    async with api() as call:
        status, resp = await call("POST", "/api/uploader/projects", _body())
    assert status == 507 and resp["needed"] == 1000
    assert _projects() == 0
    assert db.meta_get(f"uploader_job_{JOB}") is None


@pytest.mark.asyncio
async def test_create_validates_body():
    async with api() as call:
        for bad in ({"job_id": "nothex"}, {"job_id": JOB.upper()}, {"size": 0}, {"size": "10"},
                    {"stream_date": "27/10/2026"}, {"stream_date": "2026-13-40"}, {"filename": ""},
                    {"filename": "/"}, {"duration_s": "long"}, {"mime": 5}):
            status, _ = await call("POST", "/api/uploader/projects", _body(**bad))
            assert status == 400, bad
    assert _projects() == 0


def _vod_ready_actors():
    return [r[0] for r in db.get().execute("SELECT actor FROM events WHERE kind='vod_ready'")]


async def _uploaded(call, drive):
    _, a = await call("POST", "/api/uploader/projects", _body())
    p = es.get_project(a["project_id"])
    drive["files"]["VOD"] = {"parents": [p["drive_folder_id"]], "appProperties": {"project_id": str(p["id"])},
                             "size": 1000}
    return p["id"]


@pytest.mark.asyncio
async def test_complete_marks_ready_with_agent_actor(drive):
    async with api() as call:
        pid = await _uploaded(call, drive)
        assert await call("POST", f"/api/uploader/projects/{pid}/complete", {"file_id": "VOD"}) == (
            200, {"status": "ready"})
    assert es.get_project(pid)["vod_status"] == "ready"
    assert _vod_ready_actors() == ["agent"]


@pytest.mark.asyncio
async def test_complete_twice_is_ok(drive):
    async with api() as call:
        pid = await _uploaded(call, drive)
        for _ in range(2):
            assert (await call("POST", f"/api/uploader/projects/{pid}/complete", {"file_id": "VOD"}))[0] == 200
    assert _vod_ready_actors() == ["agent"]


@pytest.mark.asyncio
async def test_complete_rejects_wrong_file(drive):
    async with api() as call:
        pid = await _uploaded(call, drive)
        drive["files"]["OTHER"] = {"parents": ["elsewhere"], "size": 1000}
        status, resp = await call("POST", f"/api/uploader/projects/{pid}/complete", {"file_id": "OTHER"})
        assert (status, resp) == (400, {"error": "That file is not this project's upload"})
        assert (await call("POST", f"/api/uploader/projects/{pid}/complete", {"file_id": "bad/id"}))[0] == 400
        assert (await call("POST", "/api/uploader/projects/999/complete", {"file_id": "VOD"}))[0] == 404
    assert es.get_project(pid)["vod_status"] == "uploading"


@pytest.mark.asyncio
async def test_live_reports_level(monkeypatch):
    async with api() as call:
        for level in (True, False, None):
            monkeypatch.setattr(discord_live, "live_state", lambda now=None, v=level: {"live": v, "title": "t"})
            assert await call("GET", "/api/uploader/live") == (200, {"live": level})
