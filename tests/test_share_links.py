"""Plan 6E: share links (Drive anyone-permission, expiry, revoke, untrusted clock) and guest notes."""
import contextlib
import hashlib

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core import auth, db, scheduler
from core.config import config
from editor import editor_api, editor_bell, editor_comments as ec, editor_store as es, gdrive, notifier
from editor import share_api, share_links as sl

T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def drive(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    scheduler._reset_for_tests()
    sl._recent.clear()
    monkeypatch.setattr(config, "_data", {"authorized_github_usernames": ["aryan"], "editor_github_username": "cutguy",
                                          "public_base_url": "https://hub.example", "gdrive_public_api_key": "PUBKEY"})
    s = {"shared": [], "unshared": [], "fail_unshare": False}

    async def share_anyone(fid):
        s["shared"].append(fid)
        return f"PERM{len(s['shared'])}"

    async def unshare(fid, pid):
        if s["fail_unshare"]:
            raise gdrive.DriveError("Drive request failed (500)", 500)
        s["unshared"].append((fid, pid))

    monkeypatch.setattr(gdrive, "share_anyone", share_anyone)
    monkeypatch.setattr(gdrive, "unshare", unshare)
    return s


def _version():
    p = es.create_project("Ace", "2026-10-20", "", "vod.mp4", 5, now=1.0)
    t = es.create_tape(p["id"], "Clutch", now=1.0)
    it = es.create_iteration(t["id"], "a.mp4", 10, "", now=2.0)
    es.complete_iteration(it["id"], "FILE1", now=3.0)
    es.set_iteration_media(it["id"], checks_json='{"duration_s": 90, "codec": "hvc1", "playable": true}')
    return t, es.get_iteration(it["id"])


def _token(url):
    assert url.startswith("https://hub.example/s#")
    return url.split("#", 1)[1]


@pytest.mark.asyncio
async def test_create_shares_one_file_and_stores_only_the_hash(drive):
    t, it = _version()
    got = await sl.create(it["id"], 2, "aryan", now=T0)
    token = _token(got["url"])
    assert got["expires_at"] == T0 + 7200
    assert drive["shared"] == ["FILE1"]
    rows = [dict(r) for r in db.get().execute("SELECT * FROM share_links")]
    assert rows[0]["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in str(rows)


@pytest.mark.asyncio
async def test_create_refuses_other_durations_a_missing_key_and_an_unready_version(drive, monkeypatch):
    t, it = _version()
    with pytest.raises(ValueError, match="hours"):
        await sl.create(it["id"], 3, "aryan", now=T0)
    pending = es.create_iteration(t["id"], "b.mp4", 10, "", now=4.0)
    with pytest.raises(ValueError, match="uploaded version"):
        await sl.create(pending["id"], 2, "aryan", now=T0)
    monkeypatch.setitem(config._data, "gdrive_public_api_key", "")
    with pytest.raises(ValueError, match="API key"):
        await sl.create(it["id"], 2, "aryan", now=T0)
    assert drive["shared"] == []


@pytest.mark.asyncio
async def test_a_failed_insert_after_sharing_unshares_at_once(drive, monkeypatch):
    t, it = _version()
    monkeypatch.setattr(sl, "_hash", lambda tok: (_ for _ in ()).throw(RuntimeError("disk")))
    with pytest.raises(RuntimeError):
        await sl.create(it["id"], 2, "aryan", now=T0)
    assert drive["unshared"] == [("FILE1", "PERM1")]


@pytest.mark.asyncio
async def test_find_refuses_wrong_expired_revoked_deleted_and_an_untrusted_clock(drive):
    t, it = _version()
    token = _token((await sl.create(it["id"], 1, "aryan", now=T0))["url"])
    assert sl.find(token, now=T0 + 10)["iteration_id"] == it["id"]
    assert sl.find("x" * 43, now=T0 + 10) is None
    assert sl.find(token, now=T0 + 3600) is None
    db.meta_set("clock_last_seen", str(T0 + 100))
    assert sl.find(token, now=T0 + 10) is None  # clock behind what it has seen
    db.meta_set("clock_last_seen", str(T0))
    with db.transaction() as conn:
        conn.execute("UPDATE iterations SET status='deleted' WHERE id=?", (it["id"],))
    assert sl.find(token, now=T0 + 10) is None


@pytest.mark.asyncio
async def test_expiry_revokes_and_a_drive_failure_retries_with_backoff(drive):
    t, it = _version()
    await sl.create(it["id"], 1, "aryan", now=T0)
    drive["fail_unshare"] = True
    await sl.run(T0 + 3600)
    row = db.get().execute("SELECT * FROM share_links").fetchone()
    assert row["state"] == "revoking" and row["next_try"] == T0 + 3600 + 30
    await sl.run(T0 + 3610)  # not due yet
    assert db.get().execute("SELECT attempts FROM share_links").fetchone()[0] == 1
    drive["fail_unshare"] = False
    await sl.run(T0 + 3631)
    row = db.get().execute("SELECT * FROM share_links").fetchone()
    assert row["state"] == "revoked" and row["revoke_reason"] == "expired"
    assert drive["unshared"] == [("FILE1", "PERM1")]
    assert sl.list_live(T0 + 3631) == []


@pytest.mark.asyncio
async def test_the_clock_gate_shutting_revokes_every_link_once_per_pause(drive):
    t, it = _version()
    await sl.create(it["id"], 168, "aryan", now=T0)
    await sl.create(it["id"], 168, "aryan", now=T0)
    calls = []

    async def spy(now):
        calls.append(now)
        await sl.revoke_all_live(now)

    scheduler.on_paused(spy)
    scheduler._internet_checked_mono = 0.0
    scheduler._monotonic = lambda: 1.0  # the internet check is throttled away
    try:
        db.meta_set("clock_last_seen", str(T0 + 1000))
        assert await scheduler.tick(T0) is False
        assert await scheduler.tick(T0 + 30) is False
        assert len(calls) == 1
        assert len(drive["unshared"]) == 2
        scheduler.reset_clock(T0 + 2000)
        assert await scheduler.tick(T0 + 2000)
        db.meta_set("clock_last_seen", str(T0 + 5000))
        assert await scheduler.tick(T0 + 2030) is False
        assert len(calls) == 2
    finally:
        scheduler._monotonic = __import__("time").monotonic


def test_share_paths_open_by_whole_segment():
    assert auth.path_matches("/s", "/s") and auth.path_matches("/api/share/view", "/api/share")
    assert not auth.path_matches("/share-admin", "/s") and not auth.path_matches("/api/sharex", "/api/share")


class _Api:
    def __init__(self, client):
        self.client = client
        self.tokens = {"admin": auth.create_session("aryan", "admin"), "editor": auth.create_session("cutguy", "editor")}

    async def call(self, who, method, path, json=None):
        headers = {"Cookie": f"{auth.SESSION_COOKIE_NAME}={self.tokens[who]}"} if who else {}
        resp = await self.client.request(method, path, json=json, headers=headers)
        return resp.status, await resp.json()


@contextlib.asynccontextmanager
async def api():
    app = web.Application(middlewares=[auth.auth_middleware])
    editor_api.register_routes(app)
    share_api.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield _Api(client)
    finally:
        await client.close()


async def _link(a, it, hours=2):
    status, got = await a.call("admin", "POST", f"/api/editor/iterations/{it['id']}/share", {"hours": hours})
    assert status == 201, got
    return _token(got["url"])


def _note(token, cid="client-note-01", **over):
    return {"token": token, "name": "Rahul", "kind": "fix", "body": "too slow", "start_s": 4.0, "client_id": cid, **over}


@pytest.mark.asyncio
async def test_only_the_streamer_makes_lists_revokes_and_approves(drive):
    t, it = _version()
    async with api() as a:
        token = await _link(a, it)
        for method, path, body in [("POST", f"/api/editor/iterations/{it['id']}/share", {"hours": 2}),
                                   ("GET", "/api/editor/share-links", None),
                                   ("POST", "/api/editor/share-links/1/revoke", None),
                                   ("GET", f"/api/editor/iterations/{it['id']}/guest-notes", None),
                                   ("POST", "/api/editor/guest-notes/approve", {"ids": [1]}),
                                   ("POST", "/api/editor/guest-notes/reject", {"ids": [1]})]:
            assert (await a.call("editor", method, path, body))[0] == 403, path
        _, live = await a.call("admin", "GET", "/api/editor/share-links")
        assert [x["tape"] for x in live["links"]] == ["Clutch"] and token not in str(live)
        assert (await a.call("admin", "POST", "/api/editor/share-links/1/revoke"))[1] == {"revoked": True}
        assert (await a.call(None, "POST", "/api/share/view", {"token": token}))[0] == 404


@pytest.mark.asyncio
async def test_guest_view_gives_the_file_and_key_and_one_404_for_every_failure(drive):
    t, it = _version()
    async with api() as a:
        token = await _link(a, it)
        status, view = await a.call(None, "POST", "/api/share/view", {"token": token})
        assert status == 200
        assert view == {"title": "Clutch v1", "file_id": "FILE1", "api_key": "PUBKEY", "expires_at": view["expires_at"],
                        "duration_s": 90, "codec": "hvc1", "playable": True}
        for bad in ({"token": "nope"}, {"token": "y" * 43}, {}):
            assert await a.call(None, "POST", "/api/share/view", bad) == (404, {"error": sl.GONE})


@pytest.mark.asyncio
async def test_guest_notes_are_idempotent_validated_and_rate_limited(drive, monkeypatch):
    t, it = _version()
    async with api() as a:
        token = await _link(a, it)
        assert (await a.call(None, "POST", "/api/share/notes", _note(token)))[0] == 201
        assert (await a.call(None, "POST", "/api/share/notes", _note(token)))[0] == 201  # same row back
        assert db.get().execute("SELECT COUNT(*) FROM guest_notes").fetchone()[0] == 1
        assert (await a.call(None, "POST", "/api/share/notes", _note(token, "client-note-02", name="")))[0] == 400
        assert (await a.call(None, "POST", "/api/share/notes", _note(token, "client-note-03", kind="love")))[0] == 400
        monkeypatch.setattr(sl, "BURST", 2)
        assert (await a.call(None, "POST", "/api/share/notes", _note(token, "client-note-04")))[0] == 201
        assert (await a.call(None, "POST", "/api/share/notes", _note(token, "client-note-05")))[0] == 429


@pytest.mark.asyncio
async def test_approved_notes_reach_the_editor_as_the_streamers_news_and_pending_ones_never_do(drive):
    t, it = _version()
    async with api() as a:
        token = await _link(a, it)
        for i, body in enumerate(("one", "two", "three")):
            await a.call(None, "POST", "/api/share/notes", _note(token, f"client-note-1{i}", body=body))
        await a.call(None, "POST", "/api/share/notes", _note(token, "client-note-20", name="Priya", body="four"))
        _, got = await a.call("admin", "GET", f"/api/editor/iterations/{it['id']}/guest-notes")
        assert [(g["name"], [n["body"] for n in g["notes"]]) for g in got["guests"]] == [
            ("Rahul", ["one", "two", "three"]), ("Priya", ["four"])]
        ids = [n["id"] for n in got["guests"][0]["notes"]]
        _, seen_before = await a.call("editor", "GET", f"/api/editor/iterations/{it['id']}")
        assert seen_before["comments"] == []
        assert (await a.call("admin", "POST", "/api/editor/guest-notes/approve", {"ids": ids[:2]}))[1] == {"approved": 2}
        assert (await a.call("admin", "POST", "/api/editor/guest-notes/approve", {"ids": ids[:2]}))[1] == {"approved": 2}
        assert (await a.call("admin", "POST", "/api/editor/guest-notes/reject", {"ids": [ids[2]]}))[1] == {"rejected": 1}
        _, view = await a.call("editor", "GET", f"/api/editor/iterations/{it['id']}")
    assert [(c["author"], c["body"]) for c in view["comments"]] == [("guest:Rahul", "one"), ("guest:Rahul", "two")]
    assert "three" not in str(ec.search("three")) and "four" not in str(ec.search("four"))
    added = [e for e in es.events_since(0) if e["kind"] == "comment_added"]
    assert [e["actor"] for e in added] == ["aryan", "aryan"]
    assert sl.pending(it["id"])[0]["name"] == "Priya"


@pytest.mark.asyncio
async def test_guest_notes_ring_the_streamers_bell_and_digest_only(drive):
    t, it = _version()
    link = sl.find(_token((await sl.create(it["id"], 2, "aryan", now=T0))["url"]), now=T0)
    sl.add_note(link, "Rahul", "fix", "secret note text", 1.0, None, "client-bell-01", now=T0)
    admin = editor_bell.feed("aryan")["items"]
    assert [i["summary"] for i in admin if i["kind"] == "guest_notes_added"] == ["Rahul left a guest note on Clutch v1"]
    assert all(i["kind"] != "guest_notes_added" for i in editor_bell.feed("cutguy")["items"])
    assert editor_bell.feed("cutguy")["unseen"] == 0
    db.meta_set("notify_cursor", "0")
    await notifier.run(T0)
    rows = [dict(r) for r in db.get().execute("SELECT recipient_role FROM digest_pending")]
    assert rows == [{"recipient_role": "streamer"}]
    assert notifier._count("guest_notes_added", 3) == "3 guest notes to approve"
    rows = db.get().execute("SELECT d.actor, e.kind, e.project_id, e.tape_id, e.iteration_id FROM digest_pending d"
                            " JOIN events e ON e.id=d.event_id").fetchall()
    assert notifier._digest(rows) == "Rahul: 1 guest note to approve on Clutch v1."
