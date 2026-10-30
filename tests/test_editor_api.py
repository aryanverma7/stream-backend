import base64
import contextlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core import auth
from core import db
from editor import deletions
from editor import editor_api
from editor import editor_comments as ec
from editor import editor_personal as ep
from editor import editor_store as es
from editor import gdrive
from core import scheduler
from core.config import config

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
        s["files"]["pending"] = {"parents": [parent], "appProperties": props, "size": size,
                                 "name": name, "mime": mime, "origin": origin}
        return s.get("next_url", "https://upload/s")

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
async def test_only_the_editors_download_click_is_recorded(monkeypatch):
    p, _, _, _ = _world()

    async def down(*a, **k):
        raise gdrive.DriveError("down", 500)
    # No Drive call and no token: a Drive outage must not lose the click.
    monkeypatch.setattr(gdrive, "access_token", down)
    monkeypatch.setattr(gdrive, "get_file", down)
    async with api() as a:
        status, got = await a.admin("POST", f"/api/editor/projects/{p['id']}/download")
        assert (status, got) == (200, {"ok": True})
        assert es.get_project(p["id"])["download_clicked_at"] is None
        status, got = await a.editor("POST", f"/api/editor/projects/{p['id']}/download")
        assert (status, got) == (200, {"ok": True})
    assert es.get_project(p["id"])["download_clicked_at"] is not None


@pytest.mark.asyncio
async def test_media_link_refuses_files_that_are_not_ready_and_maps_drive_404(drive, monkeypatch):
    _, t, it, _ = _world()
    gone = es.create_iteration(t["id"], "b.mp4", 10, "")
    es.complete_iteration(gone["id"], "IT2")
    db.get().execute("UPDATE iterations SET status='deleted' WHERE id=?", (gone["id"],))
    async with api() as a:
        assert await a.editor("GET", f"/api/editor/iterations/{gone['id']}/media-link") == (
            409, {"error": "This file is not available"})

        async def missing(fid):
            raise gdrive.DriveError("File not found", 404)
        monkeypatch.setattr(gdrive, "get_file", missing)
        assert await a.editor("GET", f"/api/editor/iterations/{it['id']}/media-link") == (
            404, {"error": "This file is no longer on Drive"})

        async def offline():
            raise gdrive.DriveNotConnected("no")
        monkeypatch.setattr(gdrive, "access_token", offline)
        assert (await a.editor("GET", f"/api/editor/iterations/{it['id']}/media-link"))[0] == 503


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
        assert (await a.editor("GET", "/api/editor/me"))[1] == {"username": "cutguy", "role": "editor",
                                                               "brand_kit_url": None}


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


@pytest.mark.asyncio
async def test_editor_cannot_delete_a_project():
    p, _, _, _ = _world()
    async with api() as a:
        status, got = await a.editor("POST", "/api/editor/deletions",
                                     {"kind": "project", "target_id": p["id"], "confirm": "Ace"})
    assert (status, got) == (403, FORBIDDEN)
    assert deletions.pending() == []


@pytest.mark.asyncio
async def test_ids_and_file_ids_are_bounded():
    _, _, it, _ = _world()
    async with api() as a:
        assert (await a.admin("GET", "/api/editor/projects/" + "9" * 19))[0] == 404
        assert (await a.admin("GET", "/api/editor/iterations/" + "9" * 19))[0] == 404
        for bad in ("../etc", "a b", "x" * 201, "id?alt=media"):
            status, got = await a.editor("POST", f"/api/editor/iterations/{it['id']}/complete", {"file_id": bad})
            assert (status, got) == (400, {"error": "file_id is not a Drive file id"}), bad


@pytest.mark.asyncio
async def test_media_links_are_not_cached_and_thumbnail_is_nosniff():
    p, _, it, _ = _world()
    es.set_iteration_media(it["id"], thumbnail=b"\xff\xd8")
    async with api() as a:
        for path in (f"/api/editor/iterations/{it['id']}/media-link", f"/api/editor/projects/{p['id']}/download"):
            method = "GET" if path.endswith("media-link") else "POST"
            resp = await a.client.request(method, path, headers={
                "Cookie": f"{auth.SESSION_COOKIE_NAME}={a.tokens['editor']}"})
            assert resp.status == 200 and resp.headers.get("Cache-Control") == "no-store", path
        resp = await a.client.get(f"/api/editor/iterations/{it['id']}/thumbnail",
                                  headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}={a.tokens['editor']}"})
        assert resp.status == 200 and resp.headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.asyncio
async def test_upload_urls_only_on_the_single_iteration_get(drive):
    p, t, it, _ = _world()
    es.set_vod_upload(p["id"], "https://upload/vod")
    db.get().execute("UPDATE iterations SET upload_url='https://upload/it' WHERE id=?", (it["id"],))
    async with api() as a:
        assert all("vod_upload_url" not in x for x in (await a.admin("GET", "/api/editor/projects"))[1]["projects"])
        assert "vod_upload_url" not in (await a.admin("GET", f"/api/editor/projects/{p['id']}"))[1]["project"]
        assert "vod_upload_url" not in (await a.admin("PATCH", f"/api/editor/projects/{p['id']}", {"name": "N"}))[1]
        assert all("upload_url" not in i for i in (await a.admin("GET", f"/api/editor/tapes/{t['id']}"))[1]["iterations"])
        got = (await a.admin("GET", f"/api/editor/iterations/{it['id']}"))[1]["iteration"]
        assert got["upload_url"] == "https://upload/it"


@pytest.mark.asyncio
async def test_deleted_projects_tapes_and_iterations_are_404():
    p, t, it, c = _world()
    db.get().execute("UPDATE projects SET deleted_at=9 WHERE id=?", (p["id"],))
    async with api() as a:
        for method, path in (("GET", f"/api/editor/tapes/{t['id']}"),
                             ("POST", f"/api/editor/tapes/{t['id']}/iterations"),
                             ("GET", f"/api/editor/iterations/{it['id']}"),
                             ("GET", f"/api/editor/iterations/{it['id']}/media-link"),
                             ("PATCH", f"/api/editor/comments/{c['id']}")):
            status, _ = await a.editor(method, path, {"filename": "x", "size": 1, "mime": "v", "body": "b"})
            assert status == 404, (method, path)


@pytest.mark.asyncio
async def test_undo_of_missing_request_is_404_and_tape_creation_is_logged():
    p, _, _, _ = _world()
    async with api() as a:
        assert (await a.admin("POST", "/api/editor/deletions/999/undo"))[0] == 404
        status, t = await a.editor("POST", f"/api/editor/projects/{p['id']}/tapes", {"name": "Second"})
        assert status == 201
    [ev] = [e for e in es.events_since(0) if e["kind"] == "tape_created"]
    assert (ev["actor"], ev["tape_id"], ev["project_id"]) == ("cutguy", t["id"], p["id"])


def _clips(*names):
    return [es.create_project(n, "2026-10-20", "", f"{n}.mp4", 5, now=float(i), duration_s=30.0)
            for i, n in enumerate(names)]


@pytest.mark.asyncio
async def test_clip_payloads_combine_and_uncombine_over_the_api():
    a, b = _clips("A", "B")
    async with api() as x:
        status, body = await x.editor("GET", f"/api/editor/projects/{a['id']}")
        assert status == 200 and body["project"]["kind"] == "clip" and body["project"]["duration_s"] == 30.0
        assert body["project"]["reel_tape_id"] == a["reel_tape_id"]
        assert [(t["kind"], t["source_project_ids"]) for t in body["tapes"]] == [("reel", [a["id"]])]
        status, body = await x.editor("GET", "/api/editor/projects")
        assert {p["kind"] for p in body["projects"]} == {"clip"}
        status, reel = await x.editor("POST", "/api/editor/clips/combine",
                                      {"project_ids": [a["id"], b["id"]], "name": "Duo"})
        assert status == 200 and reel["source_project_ids"] == [a["id"], b["id"]] and reel["name"] == "Duo"
        status, body = await x.admin("GET", f"/api/editor/tapes/{reel['id']}")
        assert body["tape"]["kind"] == "reel" and body["tape"]["source_project_ids"] == [a["id"], b["id"]]
        for bad in ({"project_ids": [a["id"]], "name": "x"}, {"project_ids": "1,2", "name": "x"},
                    {"project_ids": [a["id"], True], "name": "x"}, {"project_ids": [a["id"], b["id"]]}):
            assert (await x.editor("POST", "/api/editor/clips/combine", bad))[0] == 400, bad
        status, body = await x.editor("POST", f"/api/editor/tapes/{reel['id']}/uncombine")
        assert status == 200 and [r["source_project_ids"] for r in body["reels"]] == [[a["id"]], [b["id"]]]
        assert (await x.editor("POST", f"/api/editor/tapes/{reel['id']}/uncombine"))[0] == 400


@pytest.mark.asyncio
async def test_second_reel_on_a_clip_is_refused_over_the_api():
    [a] = _clips("A")
    async with api() as x:
        assert await x.admin("POST", f"/api/editor/projects/{a['id']}/tapes", {"name": "Two"}) == \
            (400, {"error": "A clip has exactly one reel"})


@pytest.mark.asyncio
async def test_editor_cannot_start_over():
    p, t, it, c = _world()
    async with api() as x:
        assert await x.editor("POST", f"/api/editor/iterations/{it['id']}/start-over") == (403, FORBIDDEN)
        assert not ec.comments_for_iteration(it["id"])[0]["resolved"]
        assert await x.admin("POST", f"/api/editor/iterations/{it['id']}/start-over") == (200, {"resolved": 1})
        assert ec.comments_for_iteration(it["id"])[0]["resolved"]
        assert (await x.admin("POST", "/api/editor/iterations/999/start-over"))[0] == 404


@pytest.mark.asyncio
async def test_start_over_only_on_the_latest_version():
    p, t, it, c = _world()
    v2 = es.create_iteration(t["id"], "b.mp4", 10, "", now=5.0)  # still uploading: v1 is still the one in review
    async with api() as x:
        assert await x.admin("POST", f"/api/editor/iterations/{v2['id']}/start-over") == \
            (400, {"error": "Start over works on the latest version"})
        es.complete_iteration(v2["id"], "IT2", now=6.0)
        assert await x.admin("POST", f"/api/editor/iterations/{it['id']}/start-over") == \
            (400, {"error": "Start over works on the latest version"})
        assert not ec.comments_for_iteration(it["id"])[0]["resolved"]
        assert await x.admin("POST", f"/api/editor/iterations/{v2['id']}/start-over") == (200, {"resolved": 0})


@pytest.mark.asyncio
async def test_storage_includes_discord_status():
    async with api() as a:
        status, got = await a.admin("GET", "/api/editor/storage")
    assert status == 200
    assert got["discord"] == {"enabled": False, "pending": 0, "sending": 0, "dropped": 0,
                              "last_error": None, "last_sent_at": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure, message", [("not_connected", editor_api.NOT_CONNECTED), ("error", editor_api.DRIVE_PROBLEM)])
async def test_storage_without_drive_still_reports_scheduler_and_discord(drive, monkeypatch, failure, message):
    if failure == "error":
        async def about():
            raise gdrive.DriveError("x", 500)
        monkeypatch.setattr(gdrive, "about", about)
    else:
        drive["not_connected"] = True
    async with api() as a:
        status, got = await a.admin("GET", "/api/editor/storage")
    assert status == 200
    assert (got["quota"], got["files"], got["drive_error"]) == (None, None, message)
    assert got["scheduler"]["sane"] is True and got["discord"]["enabled"] is False and got["pending_deletes"] == []


@pytest.mark.asyncio
async def test_notifications_route_uses_session_identity():
    by_editor = es.record_event("cutguy", "tape_created", "cutguy added tape", now=1.0)
    by_admin = es.record_event("aryan", "tape_created", "aryan added tape", now=2.0)
    db.meta_set("bell_seen_aryan", str(by_editor))
    async with api() as a:
        status, got = await a.editor("GET", "/api/editor/notifications?username=aryan")
        assert status == 200
        assert ([i["id"] for i in got["items"]], got["unseen"]) == ([by_admin], 1)
        status, got = await a.editor("POST", "/api/editor/notifications/seen",
                                     {"up_to_id": by_admin, "username": "aryan"})
        assert status == 200
        assert db.meta_get("bell_seen_cutguy") == str(by_admin)
        assert db.meta_get("bell_seen_aryan") == str(by_editor)
        status, got = await a.admin("GET", "/api/editor/notifications")
        assert (status, [i["id"] for i in got["items"]], got["unseen"]) == (200, [by_editor], 0)
        assert (await a.admin("GET", f"/api/editor/notifications?after={by_editor}"))[1]["items"] == []
        assert (await a.admin("GET", "/api/editor/notifications?after=x"))[0] == 400
        for bad in ({}, {"up_to_id": "3"}, {"up_to_id": True}, {"up_to_id": -1},
                    {"up_to_id": 10**19}):
            assert (await a.admin("POST", "/api/editor/notifications/seen", bad))[0] == 400, bad


@pytest.mark.asyncio
async def test_new_session_for_an_unfinished_upload(drive):
    _, t, _, _ = _world()
    async with api() as a:
        status, up = await a.editor("POST", f"/api/editor/tapes/{t['id']}/iterations",
                                    {"filename": "b.mp4", "size": 20, "mime": "video/mp4"})
        assert status == 200
        drive["next_url"] = "https://upload/fresh"
        path = f"/api/editor/iterations/{up['iteration_id']}/session"
        status, got = await a.editor("POST", path, {"mime": "video/mp4"})
        assert (status, got) == (200, {"upload_url": "https://upload/fresh"})
        assert drive["files"]["pending"] == {"parents": ["TF"], "size": 20, "name": "v2 - b.mp4", "mime": "video/mp4",
                                             "appProperties": {"iteration_id": str(up["iteration_id"])},
                                             "origin": "https://x.example"}
        assert es.get_iteration(up["iteration_id"])["upload_url"] == "https://upload/fresh"
        assert (await a.admin("POST", path))[0] == 200  # no body: mime is optional
        await a.editor("POST", f"/api/editor/iterations/{up['iteration_id']}/complete", {"file_id": "IT2"})
        status, got = await a.editor("POST", path, {})
        assert (status, got) == (409, {"error": "This version is not uploading"})
        abandoned = es.create_iteration(t["id"], "d.mp4", 7, "https://upload/old")
        db.get().execute("UPDATE iterations SET status='abandoned' WHERE id=?", (abandoned["id"],))
        status, got = await a.editor("POST", f"/api/editor/iterations/{abandoned['id']}/session", {})
        assert (status, got) == (200, {"upload_url": "https://upload/fresh"})
        assert drive["files"]["pending"]["name"] == "v3 - d.mp4"
        row = es.get_iteration(abandoned["id"])
        assert (row["status"], row["upload_url"]) == ("uploading", "https://upload/fresh")
        t2 = es.create_tape(t["project_id"], "Other")
        es.complete_iteration(es.create_iteration(t2["id"], "o.mp4", 5, "")["id"], "O1")
        deletions.request_delete("tape_files", t2["id"], "Other", username="aryan", role="admin", now=5.0)
        up3 = es.create_iteration(t2["id"], "c.mp4", 5, "")
        status, got = await a.editor("POST", f"/api/editor/iterations/{up3['id']}/session", {})
        assert (status, got) == (400, {"error": "This tape's files are being deleted"})


@pytest.mark.asyncio
async def test_abandon_an_unfinished_upload(drive):
    _, t, done, _ = _world()
    async with api() as a:
        status, up = await a.editor("POST", f"/api/editor/tapes/{t['id']}/iterations",
                                    {"filename": "b.mp4", "size": 20, "mime": "video/mp4"})
        path = f"/api/editor/iterations/{up['iteration_id']}/abandon"
        status, got = await a.editor("POST", path)
        assert status == 200 and got["status"] == "abandoned"
        assert es.get_iteration(up["iteration_id"])["status"] == "abandoned"
        assert await a.admin("POST", path) == (409, {"error": "This version is not uploading"})
        assert (await a.admin("POST", f"/api/editor/iterations/{done['id']}/abandon"))[0] == 409
        assert (await a.admin("POST", "/api/editor/iterations/999/abandon"))[0] == 404


@pytest.mark.asyncio
async def test_home_buckets():
    p, t, it, c = _world()  # VOD ready, not clicked; Clutch v1 has one open comment
    clicked = es.create_project("Clicked", "2026-10-21", "", "c.mp4", 5, now=2.0)
    es.mark_vod_ready(clicked["id"], "VOD2")
    es.record_download_click(clicked["id"])
    asked = es.create_project("Asked", "2026-10-22", "", "d.mp4", 5, now=3.0)
    es.mark_vod_ready(asked["id"], "VOD3")
    es.record_download_click(asked["id"])
    deletions.request_delete("vod", asked["id"], "d.mp4", username="cutguy", role="editor", now=5.0)
    # A second tape with two open comments on v2, and a resolved one on the earlier v1.
    t2 = es.create_tape(p["id"], "Ace round")
    v1 = es.create_iteration(t2["id"], "x.mp4", 1, "")
    es.complete_iteration(v1["id"], "X1")
    for body in ("one", "two"):
        ec.add_comment(v1["id"], "aryan", "fix", body)
    v2 = es.create_iteration(t2["id"], "y.mp4", 1, "")
    es.complete_iteration(v2["id"], "X2")
    ec.carry_over(v2["id"])
    es.create_iteration(t2["id"], "z.mp4", 1, "")  # v3 still uploading: not the latest READY one
    # A project being deleted shows nowhere.
    gone = es.create_project("Gone", "2026-10-23", "", "g.mp4", 5, now=4.0)
    es.mark_vod_ready(gone["id"], "VOD4")
    gt = es.create_tape(gone["id"], "G")
    gi = es.create_iteration(gt["id"], "g.mp4", 1, "")
    es.complete_iteration(gi["id"], "G1")
    ec.add_comment(gi["id"], "aryan", "fix", "x")
    deletions.request_delete("project", gone["id"], "Gone", username="aryan", role="admin", now=5.0)
    async with api() as a:
        for who in ("admin", "editor"):
            status, got = await a.call(who, "GET", "/api/editor/home")
            assert status == 200
            assert [x["name"] for x in got["vods_ready"]] == ["Ace"]
            assert [x["name"] for x in got["deletes_to_confirm"]] == ["Clicked"]
            assert all("vod_upload_url" not in x for x in got["vods_ready"] + got["deletes_to_confirm"])
            assert [(x["tape"]["name"], x["project_name"], x["latest_iteration_id"], x["latest_version"],
                     x["open_comments"]) for x in got["open_tapes"]] == [
                ("Ace round", "Ace", v2["id"], 2, 2), ("Clutch", "Ace", it["id"], 1, 1)]


@pytest.mark.asyncio
async def test_fix_list_is_the_latest_versions_unresolved_comments():
    p, t, it, c = _world()
    done = ec.add_comment(it["id"], "aryan", "fix", "resolved already", now=5.0)
    ec.set_resolved(done["id"], it["id"], True, "aryan")
    t2 = es.create_tape(p["id"], "Ace round")
    v1 = es.create_iteration(t2["id"], "x.mp4", 1, "")
    es.complete_iteration(v1["id"], "X1")
    old = ec.add_comment(v1["id"], "aryan", "fix", "carried", now=3.5)
    fixed = ec.add_comment(v1["id"], "aryan", "fix", "fixed in v1", now=6.0)
    ec.set_resolved(fixed["id"], v1["id"], True, "aryan")
    v2 = es.create_iteration(t2["id"], "y.mp4", 1, "")
    es.complete_iteration(v2["id"], "X2")
    ec.carry_over(v2["id"])
    new = ec.add_comment(v2["id"], "cutguy", "question", "new on v2", now=7.0)
    gone = es.create_project("Gone", "2026-10-23", "", "g.mp4", 5, now=4.0)
    es.mark_vod_ready(gone["id"], "GV", now=4.0)
    gt = es.create_tape(gone["id"], "G")
    gi = es.create_iteration(gt["id"], "g.mp4", 1, "")
    es.complete_iteration(gi["id"], "G1")
    ec.add_comment(gi["id"], "aryan", "fix", "in limbo", now=1.0)
    deletions.request_delete("project", gone["id"], "Gone", username="aryan", role="admin", now=5.0)
    async with api() as a:
        for who in ("admin", "editor"):
            status, got = await a.call(who, "GET", "/api/editor/fix-list")
            assert status == 200
            assert [(i["comment"]["id"], i["tape_id"], i["tape_name"], i["project_id"], i["iteration_id"],
                     i["version"]) for i in got["items"]] == [
                (old["id"], t2["id"], "Ace round", p["id"], v2["id"], 2),
                (c["id"], t["id"], "Clutch", p["id"], it["id"], 1),
                (new["id"], t2["id"], "Ace round", p["id"], v2["id"], 2)]
            assert got["items"][0]["comment"]["body"] == "carried"


@pytest.mark.asyncio
async def test_me_carries_the_brand_kit_link():
    async with api() as a:
        assert (await a.editor("GET", "/api/editor/me"))[1] == {"username": "cutguy", "role": "editor",
                                                               "brand_kit_url": None}
        db.meta_set("gdrive_brand_kit", "BK1")
        assert (await a.admin("GET", "/api/editor/me"))[1]["brand_kit_url"] == \
            "https://drive.google.com/drive/folders/BK1"


@pytest.mark.asyncio
async def test_drive_space_both_roles_unlimited_and_errors(drive):
    drive["quota"] = {"limit": 1000, "usage": 250}
    async with api() as a:
        for call in (a.admin, a.editor):
            assert await call("GET", "/api/editor/drive/space") == (200, {"limit": 1000, "usage": 250, "free": 750})
        drive["quota"] = {"limit": None, "usage": 5}
        assert (await a.editor("GET", "/api/editor/drive/space"))[1] == {"limit": None, "usage": 5, "free": None}
        drive["not_connected"] = True
        assert (await a.editor("GET", "/api/editor/drive/space"))[0] == 503


@pytest.mark.asyncio
async def test_comment_route_201_then_200_on_repeat():
    _, _, it, _ = _world()
    path = f"/api/editor/iterations/{it['id']}/comments"
    async with api() as a:
        s1, g1 = await a.editor("POST", path, {"kind": "fix", "body": "x", "client_id": "abcdefgh1"})
        s2, g2 = await a.editor("POST", path, {"kind": "fix", "body": "x", "client_id": "abcdefgh1"})
        assert (s1, s2, g1["id"] == g2["id"]) == (201, 200, True)


@pytest.mark.asyncio
async def test_someone_elses_client_id_is_a_409():
    _, _, it, _ = _world()
    path = f"/api/editor/iterations/{it['id']}/comments"
    async with api() as a:
        assert (await a.editor("POST", path, {"kind": "fix", "body": "x", "client_id": "abcdefgh1"}))[0] == 201
        assert await a.admin("POST", path, {"kind": "fix", "body": "x", "client_id": "abcdefgh1"}) == (
            409, {"error": "client_id already used"})


@pytest.mark.asyncio
async def test_comment_route_rejects_bad_client_id():
    _, _, it, _ = _world()
    async with api() as a:
        status, got = await a.editor("POST", f"/api/editor/iterations/{it['id']}/comments",
                                     {"kind": "fix", "body": "x", "client_id": "x"})
        assert (status, got) == (400, {"error": "client_id is not valid"})


@pytest.mark.asyncio
async def test_seen_route_both_roles():
    _, _, it, _ = _world()
    path = f"/api/editor/iterations/{it['id']}/seen"
    async with api() as a:
        for who in ("admin", "editor"):
            assert await a.call(who, "POST", path) == (200, {"previous_seen_at": None})
            status, got = await a.call(who, "POST", path)
            assert status == 200 and isinstance(got["previous_seen_at"], float)


@pytest.mark.asyncio
async def test_reply_route_on_old_db_without_client_id_column():
    _, _, it, c = _world()  # creates the tables with the current DDL; rebuild them old-style
    db.get().executescript("""DROP INDEX IF EXISTS comments_client_id; DROP INDEX IF EXISTS replies_client_id;
        ALTER TABLE comments DROP COLUMN client_id; ALTER TABLE replies DROP COLUMN client_id;""")
    db._ensured.clear()  # a fresh process would migrate again
    path = f"/api/editor/comments/{c['id']}/replies"
    async with api() as a:
        assert (await a.editor("POST", path, {"body": "x", "client_id": "abcdefgh1"}))[0] == 201
        assert (await a.editor("POST", path, {"body": "x", "client_id": "abcdefgh1"}))[0] == 200
        status, got = await a.editor("POST", path, {"body": "x", "client_id": "x"})
        assert (status, got) == (400, {"error": "client_id is not valid"})
