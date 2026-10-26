import base64
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import auth
import db
import deletions
import editor_api
import editor_comments as ec
import editor_personal as ep
import editor_store as es
import gdrive
import scheduler
from config import config

FORBIDDEN = {"error": "Only the streamer can do this."}


@pytest.fixture(autouse=True)
def drive(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    scheduler._reset_for_tests()
    monkeypatch.setattr(config, "_data", {"authorized_github_usernames": ["aryan"],
                                          "editor_github_username": "cutguy",
                                          "public_base_url": "https://x.example"})
    db.meta_set("gdrive_projects", "PROJ")
    s = {"quota": {"limit": 1000, "usage": 0}, "files": {}, "folders": 0, "deleted": []}

    async def about():
        if s.get("not_connected"):
            raise gdrive.DriveNotConnected("no")
        return s["quota"]

    async def create_folder(name, parent):
        s["folders"] += 1
        return f"F{s['folders']}"

    async def start(name, parent, size, mime, props, origin):
        s["files"]["pending"] = {"parents": [parent], "appProperties": props, "size": size}
        return "https://upload/s"

    async def get_file(fid):
        return s["files"].get(fid) or s["files"].get("pending") or {}

    async def access_token():
        return "TOKEN"

    async def delete_file(fid):
        s["deleted"].append(fid)

    async def share(*a, **k):
        pass

    for name, fn in (("about", about), ("create_folder", create_folder), ("start_resumable_upload", start),
                     ("get_file", get_file), ("access_token", access_token), ("delete_file", delete_file),
                     ("share_with", share)):
        monkeypatch.setattr(gdrive, name, fn)
    return s


class _Api:
    def __init__(self, client):
        self.client = client
        self.tokens = {"admin": auth.create_session("aryan", "admin"),
                       "editor": auth.create_session("cutguy", "editor")}

    async def call(self, who, method, path, json=None, data=None):
        resp = await self.client.request(
            method, path, json=json, data=data,
            headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}={self.tokens[who]}"})
        body = await resp.json() if resp.content_type == "application/json" else await resp.read()
        return resp.status, body

    def admin(self, method, path, json=None, **kw):
        return self.call("admin", method, path, json, **kw)

    def editor(self, method, path, json=None, **kw):
        return self.call("editor", method, path, json, **kw)


@contextlib.asynccontextmanager
async def api():
    app = web.Application(middlewares=[auth.auth_middleware])
    editor_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield _Api(client)
    finally:
        await client.close()


def _world():
    """A project with a ready VOD, one tape with a ready v1 carrying one open comment by the editor."""
    p = es.create_project("Ace", "2026-10-20", "", "vod.mp4", 5, now=1.0)
    es.mark_vod_ready(p["id"], "VOD", now=1.0)
    t = es.create_tape(p["id"], "Clutch", now=1.0)
    es.set_tape_folder(t["id"], "TF")
    it = es.create_iteration(t["id"], "a.mp4", 10, "", now=2.0)
    es.complete_iteration(it["id"], "IT1", now=3.0)
    c = ec.add_comment(it["id"], "cutguy", "fix", "too long", 1.0, 2.0, now=4.0)
    return p, t, es.get_iteration(it["id"]), c


@pytest.mark.asyncio
async def test_editor_is_refused_admin_actions():
    p, t, it, c = _world()
    req = deletions.request_delete("vod", p["id"], "vod.mp4", username="aryan", role="admin", now=5.0)
    async with api() as a:
        cases = [
            ("PATCH", f"/api/editor/projects/{p['id']}", {"name": "Renamed"}),
            ("PATCH", f"/api/editor/tapes/{t['id']}", {"status": "final"}),
            ("PUT", f"/api/editor/iterations/{it['id']}/comments/{c['id']}/resolved", {"resolved": True}),
            ("POST", "/api/editor/deletions", {"kind": "tape_files", "target_id": t["id"], "confirm": "Clutch"}),
            ("GET", "/api/editor/storage", None),
            ("GET", "/api/editor/drive/status", None),
            ("POST", f"/api/editor/deletions/{req['request_id']}/undo", None),
            ("POST", "/api/editor/scheduler/reset-clock", None),
        ]
        for method, path, body in cases:
            status, got = await a.editor(method, path, body)
            assert status == 403, (method, path, status, got)
            assert "Only the streamer" in got["error"], (method, path)
        assert (await a.editor("PATCH", f"/api/editor/projects/{p['id']}", {"name": "X"}))[1] == FORBIDDEN
    assert es.get_project(p["id"])["name"] == "Ace"
    assert es.get_tape(t["id"])["status"] == "in_review"
    assert ec.comments_for_iteration(it["id"])[0]["resolved"] is False
    assert [r["kind"] for r in deletions.pending()] == ["vod"]  # undo refused, no tape_files request
    assert es.get_iteration(it["id"])["status"] == "ready"
    assert db.meta_get("clock_last_seen") is None


@pytest.mark.asyncio
async def test_admin_can_do_admin_actions(drive):
    p, t, it, c = _world()
    async with api() as a:
        assert (await a.admin("PATCH", f"/api/editor/projects/{p['id']}", {"name": "Renamed"}))[0] == 200
        assert (await a.admin("PATCH", f"/api/editor/tapes/{t['id']}", {"status": "final"}))[0] == 200
        status, _ = await a.admin("PUT", f"/api/editor/iterations/{it['id']}/comments/{c['id']}/resolved",
                                  {"resolved": True})
        assert status == 200
        status, got = await a.admin("POST", "/api/editor/deletions",
                                    {"kind": "tape_files", "target_id": t["id"], "confirm": "Clutch"})
        assert status == 202
        status, got = await a.admin("POST", f"/api/editor/deletions/{got['request_id']}/undo")
        assert (status, got["status"]) == (200, "cancelled")
        status, got = await a.admin("GET", "/api/editor/storage")
        assert status == 200 and got["quota"]["free"] == 1000 and got["scheduler"]["sane"] is True
        status, got = await a.admin("GET", "/api/editor/drive/status")
        assert status == 200 and got["connected"] is True and got["layout"] is False
        status, got = await a.admin("POST", "/api/editor/scheduler/reset-clock")
        assert status == 200 and got["last_seen"] is not None and got["sane"] is True
        drive["not_connected"] = True
        status, got = await a.admin("GET", "/api/editor/drive/status")
        assert (status, got["connected"]) == (200, False)
    assert es.get_project(p["id"])["name"] == "Renamed"
    assert es.get_tape(t["id"])["status"] == "final"
    assert ec.comments_for_iteration(it["id"])[0]["resolved"] is True
    assert deletions.pending() == []


@pytest.mark.asyncio
async def test_drive_full_gives_507_with_numbers(drive):
    _, t, _, _ = _world()
    drive["quota"] = {"limit": 1000, "usage": 900}
    async with api() as a:
        status, got = await a.editor("POST", f"/api/editor/tapes/{t['id']}/iterations",
                                     {"filename": "b.mp4", "size": 500, "mime": "video/mp4"})
    assert status == 507
    assert got == {"error": "Drive is too full for this file", "needed": 500, "free": 100}


@pytest.mark.asyncio
async def test_drive_not_connected_gives_503(drive):
    _, t, _, _ = _world()
    drive["not_connected"] = True
    async with api() as a:
        status, got = await a.editor("POST", f"/api/editor/tapes/{t['id']}/iterations",
                                     {"filename": "b.mp4", "size": 5, "mime": "video/mp4"})
    assert status == 503
    assert got == {"error": "Google Drive isn't connected - connect it from the dashboard."}


@pytest.mark.asyncio
async def test_confirm_mismatch_gives_400():
    p, _, _, _ = _world()
    async with api() as a:
        status, got = await a.admin("POST", "/api/editor/deletions",
                                    {"kind": "vod", "target_id": p["id"], "confirm": "vod.mp"})
    assert (status, got) == (400, {"error": "Type the name exactly to confirm"})
    assert deletions.pending() == []


@pytest.mark.asyncio
async def test_other_users_todo_is_404():
    async with api() as a:
        status, todo = await a.admin("POST", "/api/editor/todos", {"text": "pick music"})
        assert status == 201
        path = f"/api/editor/todos/{todo['id']}"
        assert (await a.editor("PATCH", path, {"done": True}))[0] == 404
        assert (await a.editor("DELETE", path))[0] == 404
        assert (await a.editor("GET", "/api/editor/todos"))[1] == {"todos": []}
        status, got = await a.admin("PATCH", path, {"done": True})
        assert (status, got["done"]) == (200, True)
        assert (await a.admin("DELETE", path))[0] == 200
        assert (await a.admin("PATCH", path, {"done": False}))[0] == 404


@pytest.mark.asyncio
async def test_iteration_payload_includes_carried_comments_and_position(drive):
    _, t, v1, c = _world()
    async with api() as a:
        status, up = await a.editor("POST", f"/api/editor/tapes/{t['id']}/iterations",
                                    {"filename": "b.mp4", "size": 20, "mime": "video/mp4"})
        assert status == 200 and up["version"] == 2 and up["upload_url"] == "https://upload/s"
        status, _ = await a.editor("POST", f"/api/editor/iterations/{up['iteration_id']}/complete",
                                   {"file_id": "IT2"})
        assert status == 200
        assert (await a.admin("PUT", f"/api/editor/iterations/{up['iteration_id']}/position",
                              {"position_s": 12.5}))[0] == 200
        status, got = await a.admin("GET", f"/api/editor/iterations/{up['iteration_id']}")
        assert status == 200
        assert got["position_s"] == 12.5
        assert [(x["id"], x["carried"], x["origin_version"]) for x in got["comments"]] == [(c["id"], True, 1)]
        assert "thumbnail" not in got["iteration"] and got["iteration"]["has_thumbnail"] is False
        # The editor has their own position.
        assert (await a.editor("GET", f"/api/editor/iterations/{up['iteration_id']}"))[1]["position_s"] == 0.0


@pytest.mark.asyncio
async def test_only_the_editors_download_click_is_recorded():
    p, _, _, _ = _world()
    async with api() as a:
        status, got = await a.admin("POST", f"/api/editor/projects/{p['id']}/download")
        assert status == 200 and got["access_token"] == "TOKEN" and "TOKEN" not in got["url"]
        assert es.get_project(p["id"])["download_clicked_at"] is None
        status, got = await a.editor("POST", f"/api/editor/projects/{p['id']}/download")
        assert status == 200 and got["access_token"] == "TOKEN"
    assert es.get_project(p["id"])["download_clicked_at"] is not None


@pytest.mark.asyncio
async def test_comment_times_and_kind_are_validated():
    _, _, it, _ = _world()
    path = f"/api/editor/iterations/{it['id']}/comments"
    async with api() as a:
        for bad in (True, "3", float("nan"), float("inf"), [1]):
            status, got = await a.editor("POST", path, {"kind": "fix", "body": "x", "start_s": bad})
            assert (status, got) == (400, {"error": "Times must be numbers of seconds"}), bad
        assert (await a.editor("POST", path, {"kind": "rant", "body": "x"}))[0] == 400
        assert (await a.editor("POST", path, ["not", "an", "object"]))[0] == 400
        assert (await a.editor("POST", path, data=b"{nope", ))[0] == 400
        status, got = await a.editor("POST", path, {"kind": "keep", "body": "nice", "start_s": 3, "end_s": 4.5})
        assert status == 201 and (got["start_s"], got["end_s"]) == (3.0, 4.5)


@pytest.mark.asyncio
async def test_tape_detail_counts_open_comments_and_marks_seen():
    p, t, it, c = _world()
    async with api() as a:
        status, got = await a.admin("GET", f"/api/editor/projects/{p['id']}")
        assert status == 200
        (tape,) = got["tapes"]
        assert (tape["has_new"], tape["latest_version"], tape["open_comments"]) == (True, 1, 1)
        assert (await a.admin("GET", "/api/editor/projects"))[1]["projects"][0]["has_new"] is True
        status, got = await a.admin("GET", f"/api/editor/tapes/{t['id']}")
        assert status == 200 and [i["version"] for i in got["iterations"]] == [1]
        assert (await a.admin("GET", "/api/editor/projects"))[1]["projects"][0]["has_new"] is False
        assert (await a.admin("GET", "/api/editor/projects/999"))[0] == 404
    assert ep.tape_has_new("aryan", t["id"]) is False


@pytest.mark.asyncio
async def test_media_thumbnail_and_limits():
    _, _, it, _ = _world()
    path = f"/api/editor/iterations/{it['id']}"
    async with api() as a:
        assert (await a.editor("GET", path + "/thumbnail"))[0] == 404
        assert (await a.editor("PUT", path + "/media", {"thumbnail_b64": "@@not base64@@"}))[0] == 400
        big = base64.b64encode(b"x" * (es.THUMBNAIL_MAX_BYTES + 1)).decode()
        assert (await a.editor("PUT", path + "/media", {"thumbnail_b64": big}))[0] == 413
        assert (await a.editor("PUT", path + "/media", {"checks": {"x": "y" * 20000}}))[0] == 413
        jpeg = b"\xff\xd8 jpeg"
        status, _ = await a.editor("PUT", path + "/media", {"thumbnail_b64": base64.b64encode(jpeg).decode(),
                                                            "checks": {"loudness": "ok"}})
        assert status == 200
        assert (await a.editor("GET", path + "/thumbnail"))[1] == jpeg
        assert (await a.editor("GET", path))[1]["iteration"]["checks"] == {"loudness": "ok"}


@pytest.mark.asyncio
async def test_only_the_author_edits_a_comment():
    _, _, _, c = _world()
    async with api() as a:
        assert (await a.admin("PATCH", f"/api/editor/comments/{c['id']}", {"body": "mine now"}))[0] == 403
        status, got = await a.editor("PATCH", f"/api/editor/comments/{c['id']}", {"body": "shorter"})
        assert (status, got["body"]) == (200, "shorter")
        status, _ = await a.admin("POST", f"/api/editor/comments/{c['id']}/replies", {"body": "agreed"})
        assert status == 201
        assert (await a.admin("GET", "/api/editor/search?q=agreed"))[1]["hits"][0]["kind"] == "reply"
        assert (await a.editor("GET", "/api/editor/me"))[1] == {"username": "cutguy", "role": "editor"}


@pytest.mark.asyncio
async def test_reset_clock_clamps_pending_deletes():
    import time
    p, _, _, _ = _world()
    r = deletions.request_delete("vod", p["id"], "vod.mp4", username="aryan", role="admin",
                                 now=time.time() + 10 * 86400)  # scheduled while the clock ran fast
    async with api() as a:
        assert (await a.admin("POST", "/api/editor/scheduler/reset-clock"))[0] == 200
    [row] = deletions.pending()
    assert row["id"] == r["request_id"]
    assert row["delete_at"] <= time.time() + deletions.DELETE_GRACE_SECONDS
