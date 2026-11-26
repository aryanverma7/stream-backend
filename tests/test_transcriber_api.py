import contextlib
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core import auth
from core import db
from editor import editor_store as es
from editor import gdrive
from editor import transcriber_api
from editor import transcripts as tr
from editor import uploader_api
from core.config import config

SECRET = {"X-Agent-Secret": "s3cret"}
SEGS = [{"start": 0, "end": 2, "text": "ye clutch dekho"}]


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "s3cret"})
    s = {"live": False, "drive": True}

    async def live_now():
        return s["live"]

    async def access_token():
        if not s["drive"]:
            raise gdrive.DriveNotConnected("no")
        return "DRIVETOKEN"
    monkeypatch.setattr(uploader_api, "live_now", live_now)
    monkeypatch.setattr(gdrive, "access_token", access_token)
    return s


@contextlib.asynccontextmanager
async def api():
    app = web.Application(middlewares=[auth.auth_middleware])
    transcriber_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async def call(method, path, json=None, headers=SECRET, data=None):
            resp = await client.request(method, path, json=json, data=data, headers=headers)
            call.headers = resp.headers
            return resp.status, await resp.json()
        yield call
    finally:
        await client.close()


def _queued():
    p = es.create_project("P", "2026-10-20", "", "v.mp4", 1)
    e = es.create_edit(p["id"], "t")
    it = es.create_iteration(e["id"], "cut.mp4", 1234, "u")
    it = es.complete_iteration(it["id"], "FILE1")[0]
    tr.request(it["id"], "aryan", now=1)
    return it


@pytest.mark.asyncio
async def test_every_route_refuses_without_secret():
    it = _queued()
    tr.set_settings(True, None)
    async with api() as call:
        for method, path, body in (("GET", "/api/transcriber/next", None), ("GET", "/api/transcriber/status", None),
                                   ("POST", f"/api/transcriber/jobs/{it['id']}/complete",
                                    {"token": "x", "segments": SEGS, "model": "m"}),
                                   ("POST", f"/api/transcriber/jobs/{it['id']}/release", {"token": "x"})):
            for headers in ({}, {"X-Agent-Secret": "wrong"}):
                assert await call(method, path, body, headers) == (
                    401, {"error": "Invalid or missing agent secret"}), (path, headers)
    assert tr.get(it["id"])["status"] == "queued"
    assert db.meta_get("transcriber_last_seen") is None


@pytest.mark.asyncio
async def test_paused_gives_no_job_and_changes_nothing():
    it = _queued()
    async with api() as call:
        assert await call("GET", "/api/transcriber/next") == (200, {"enabled": False, "live": False, "job": None})
    assert tr.get(it["id"])["status"] == "queued"
    assert db.meta_get("transcriber_last_seen") is not None


@pytest.mark.asyncio
async def test_live_gives_no_job(setup):
    it = _queued()
    tr.set_settings(True, None)
    setup["live"] = True
    async with api() as call:
        assert await call("GET", "/api/transcriber/next") == (200, {"enabled": True, "live": True, "job": None})
    assert tr.get(it["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_drive_not_connected_gives_no_job(setup):
    it = _queued()
    tr.set_settings(True, None)
    setup["drive"] = False
    async with api() as call:
        assert (await call("GET", "/api/transcriber/next"))[1]["job"] is None
    assert tr.get(it["id"])["status"] == "queued"


@pytest.mark.asyncio
async def test_on_and_idle_claims_job(setup):
    it = _queued()
    tr.set_settings(True, None)
    setup["live"] = None  # unknown counts as not live
    async with api() as call:
        status, resp = await call("GET", "/api/transcriber/next")
        assert status == 200 and (resp["enabled"], resp["live"]) == (True, None)
        job = resp["job"]
        assert job == {"iteration_id": it["id"], "token": tr.get(it["id"])["claim_token"], "file_id": "FILE1",
                       "access_token": "DRIVETOKEN", "name": "cut.mp4", "size": 1234}
        assert call.headers.get("Cache-Control") == "no-store"  # carries a live Drive token
        assert (await call("GET", "/api/transcriber/next"))[1]["job"] is None  # nothing else queued
        assert await call("GET", "/api/transcriber/status") == (200, {"enabled": True, "live": None})
        assert await call("POST", f"/api/transcriber/jobs/{it['id']}/complete",
                          {"token": job["token"], "segments": SEGS, "model": "hinglish-apex-ct2"}) == (200, {"ok": True})
    assert tr.get(it["id"])["segments"] == [{"start": 0.0, "end": 2.0, "text": "ye clutch dekho"}]


@pytest.mark.asyncio
async def test_complete_stale_token_409_and_bad_segments_400():
    it = _queued()
    tr.set_settings(True, None)
    async with api() as call:
        job = (await call("GET", "/api/transcriber/next"))[1]["job"]
        path = f"/api/transcriber/jobs/{it['id']}/complete"
        assert (await call("POST", path, {"token": "stale", "segments": SEGS, "model": "m"}))[0] == 409
        for bad in ({"token": job["token"], "segments": [{"start": -1, "end": 1, "text": "x"}], "model": "m"},
                    {"token": job["token"], "segments": "x", "model": "m"},
                    {"token": job["token"], "segments": SEGS, "model": 5}):
            assert (await call("POST", path, bad))[0] == 400, bad
        big = b'{"token": "' + job["token"].encode() + b'", "model": "m", "segments": [' + b" " * (4 * 1024 * 1024) + b"]}"
        assert (await call("POST", path, data=big, headers={**SECRET, "Content-Type": "application/json"}))[0] == 400
        assert (await call("POST", path, {"token": job["token"], "segments": SEGS, "model": "m"}))[0] == 200
        assert (await call("POST", "/api/transcriber/jobs/999/complete",
                           {"token": job["token"], "segments": SEGS, "model": "m"}))[0] == 409


@pytest.mark.asyncio
async def test_release_requeues_and_stale_is_409():
    it = _queued()
    tr.set_settings(True, None)
    async with api() as call:
        job = (await call("GET", "/api/transcriber/next"))[1]["job"]
        path = f"/api/transcriber/jobs/{it['id']}/release"
        assert (await call("POST", path, {"token": "stale"}))[0] == 409
        assert await call("POST", path, {"token": job["token"], "error": "CUDA out of memory"}) == (200, {"ok": True})
    row = tr.get(it["id"])
    assert (row["status"], row["attempts"], row["error"]) == ("queued", 1, "CUDA out of memory")


@pytest.mark.asyncio
async def test_open_path_without_session():
    assert any(auth.path_matches("/api/transcriber/next", p) for p in auth.OPEN_PATHS)
    async with api() as call:  # no cookie at all: the middleware lets it through to the secret check
        assert (await call("GET", "/api/transcriber/next"))[0] == 200


@pytest.mark.asyncio
async def test_tokens_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    it = _queued()
    tr.set_settings(True, None)
    async with api() as call:
        job = (await call("GET", "/api/transcriber/next"))[1]["job"]
        await call("POST", f"/api/transcriber/jobs/{it['id']}/complete", {"token": "stale", "segments": SEGS, "model": "m"})
        await call("POST", f"/api/transcriber/jobs/{it['id']}/complete",
                   {"token": job["token"], "segments": SEGS, "model": "m"})
    assert caplog.records  # something was logged
    for secret in (job["token"], "DRIVETOKEN", "s3cret"):
        assert secret not in caplog.text
