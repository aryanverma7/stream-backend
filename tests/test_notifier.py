import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import db
import editor_store as es
import notifier
from config import config

T = 1000.0


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(config, "_data", {"discord_notifications_enabled": True,
                                          "authorized_github_usernames": ["Aryan"],
                                          "editor_github_username": "ed"})


def outbox():
    return [dict(r) for r in db.get().execute(
        "SELECT recipient_role, content, dedupe_key FROM dm_outbox ORDER BY id")]


def tape_with_version(name="Jett dash", versions=1):
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    t = es.create_tape(p["id"], name, now=1.0)
    its = [es.create_iteration(t["id"], "a.mp4", 1, "u", now=1.0) for _ in range(versions)]
    return p, t, its[-1]


def ev(actor, kind, it, at, tape=None):
    tape = tape or es.get_tape(it["tape_id"])
    return es.record_event(actor, kind, "s", project_id=tape["project_id"], tape_id=tape["id"],
                           iteration_id=it["id"], now=at)


@pytest.mark.asyncio
async def test_vod_ready_dms_editor_once():
    p = es.create_project("Stream 0412", "2026-10-20", "", "v.mp4", 5, now=1.0)
    c = es.create_project("Clip 0412", "2026-10-20", "", "c.mp4", 5, now=1.0, duration_s=30)
    es.record_event("system", "vod_ready", "s", project_id=p["id"], now=T)
    es.record_event("agent", "vod_ready", "s", project_id=c["id"], now=T)
    await notifier.run(T)
    await notifier.run(T + 1)
    assert outbox() == [
        {"recipient_role": "editor", "content": "New VOD ready: Stream 0412. Open the editor page to download it.",
         "dedupe_key": f"vod_ready:{p['id']}"},
        {"recipient_role": "editor", "content": "New clip ready: Clip 0412. Open the editor page to download it.",
         "dedupe_key": f"vod_ready:{c['id']}"}]


@pytest.mark.asyncio
async def test_iteration_ready_dms_streamer():
    _, t, it = tape_with_version(versions=2)
    ev("ed", "iteration_ready", it, T)
    ev("Aryan", "iteration_ready", es.create_iteration(t["id"], "b.mp4", 1, "u", now=1.0), T)  # never to yourself
    await notifier.run(T)
    assert outbox() == [{"recipient_role": "streamer", "content": "Jett dash v2 is ready to review.",
                         "dedupe_key": f"iteration_ready:{it['id']}"}]


@pytest.mark.asyncio
async def test_own_events_never_notify_yourself():
    _, _, it = tape_with_version()
    ev("aryan", "comment_added", it, T)
    await notifier.run(T + 700)
    assert [r["recipient_role"] for r in outbox()] == ["editor"]
    assert outbox()[0]["content"] == "aryan: 1 comment on Jett dash v1."


@pytest.mark.asyncio
async def test_burst_of_comments_gives_one_digest_after_quiet():
    _, _, it = tape_with_version()
    for i in range(20):
        ev("ed", "comment_added", it, T + i * 10)
    await notifier.run(T + 700)
    assert outbox() == []
    await notifier.run(T + 791)
    assert len(outbox()) == 1
    assert outbox()[0]["content"] == "ed: 20 comments on Jett dash v1."
    assert outbox()[0]["dedupe_key"].startswith("digest:streamer:")
    await notifier.run(T + 2000)
    assert len(outbox()) == 1


@pytest.mark.asyncio
async def test_cursor_survives_restart_no_double_processing(tmp_path):
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    _, _, it = tape_with_version()
    es.record_event("system", "vod_ready", "s", project_id=p["id"], now=T)
    ev("ed", "comment_added", it, T)
    await notifier.run(T + 10)
    db.use_path_for_tests(tmp_path / "t.db")  # restart
    await notifier.run(T + 20)
    assert db.get().execute("SELECT COUNT(*) FROM digest_pending").fetchone()[0] == 1
    await notifier.run(T + 700)
    assert [r["recipient_role"] for r in outbox()] == ["editor", "streamer"]


@pytest.mark.asyncio
async def test_digest_groups_by_tape_and_version():
    _, t, it1 = tape_with_version()
    it2 = es.create_iteration(t["id"], "b.mp4", 1, "u", now=1.0)
    clip = es.create_project("Clip 0412", "2026-10-20", "", "c.mp4", 5, now=1.0, duration_s=30)
    reel = es.reel_for_clip(clip["id"])
    rit = es.create_iteration(reel["id"], "r.mp4", 1, "u", now=1.0)
    for k in ["comment_added"] * 6 + ["reply_added"] * 2 + ["comment_resolved", "start_over"]:
        ev("Aryan", k, it2, T)
    ev("Aryan", "reply_added", it1, T)
    ev("Aryan", "comment_added", rit, T)
    ev("ed", "comment_added", it2, T)  # goes to the streamer, not in the editor's digest
    await notifier.run(T + 600)
    assert outbox()[0] == {
        "recipient_role": "editor",
        "content": "Aryan: 6 comments, 2 replies, 1 resolved, started over on Jett dash v2;"
                   " 1 reply on Jett dash v1; 1 comment on Clip 0412 v1.",
        "dedupe_key": "digest:editor:" + str(db.get().execute(
            "SELECT MAX(id) FROM events WHERE actor='Aryan'").fetchone()[0])}
    assert outbox()[1]["content"] == "ed: 1 comment on Jett dash v2."


@pytest.mark.asyncio
async def test_system_actor_digests_nothing():
    _, _, it = tape_with_version()
    for actor in ("system", "agent", "stranger"):
        ev(actor, "comment_added", it, T)
    ev("ed", "tape_created", it, T)
    await notifier.run(T + 10_000)
    assert outbox() == []
    assert db.meta_get("notify_cursor") == str(db.get().execute("SELECT MAX(id) FROM events").fetchone()[0])


@pytest.mark.asyncio
async def test_failure_stops_at_event_and_never_raises(monkeypatch):
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    e1 = es.record_event("system", "vod_ready", "s", project_id=p["id"], now=T)
    es.record_event("system", "vod_ready", "s", project_id=p["id"], now=T)
    real = es.get_project
    monkeypatch.setattr(es, "get_project", lambda i: (_ for _ in ()).throw(RuntimeError("boom")))
    await notifier.run(T)
    assert db.meta_get("notify_cursor") is None
    monkeypatch.setattr(es, "get_project", real)
    await notifier.run(T)
    assert len(outbox()) == 1 and db.meta_get("notify_cursor") == str(e1 + 1)


@pytest.mark.asyncio
async def test_event_for_a_missing_row_is_skipped_not_wedged():
    es.record_event("system", "vod_ready", "s", project_id=999, now=T)
    es.record_event("ed", "iteration_ready", "s", iteration_id=999, now=T)
    await notifier.run(T)
    assert outbox() == [] and db.meta_get("notify_cursor") == "2"
