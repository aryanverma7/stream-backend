import calendar
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import db
from core.config import config
from editor import editor_quota as eq
from editor import editor_store as es
from tests.test_editor_api import api, drive  # noqa: F401  (drive: the fake Drive + config fixture)

D = 86400


def ts(y, m, d, h=12):
    return calendar.timegm((y, m, d, h, 0, 0))


MON = ts(2026, 11, 16)  # Monday 16 Nov 2026


@pytest.fixture(autouse=True)
def utc(monkeypatch, drive):  # noqa: F811
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    config._data.update({"discord_notifications_enabled": True})
    yield
    monkeypatch.undo()
    time.tzset()


def project(name="Ace"):
    p = es.create_project(name, "2026-11-16", "", "v.mp4", 1, now=MON)
    es.mark_vod_ready(p["id"], "VOD", now=MON)
    return p


def deliver(p, kind, uploaded, finalised=None, name="cut"):
    """An edit (or a tape's reel) with v1 uploaded at `uploaded`, marked Final at `finalised`."""
    if kind == "reel":
        tape = es.create_project(name, "2026-11-16", "", "c.mp4", 1, now=uploaded, duration_s=30)
        t = es.reel_for_tape(tape["id"])
    else:
        t = es.create_edit(p["id"], name, now=uploaded)
        es.detect_kind(t, *((1080, 1920, 60) if kind == "clip" else (1920, 1080, 600)))
    it = es.create_iteration(t["id"], "a.mp4", 1, "", now=uploaded)
    es.complete_iteration(it["id"], f"F{it['id']}", now=uploaded)
    es.update_edit(t["id"], status="final", now=finalised or uploaded)
    return es.get_edit(t["id"])


def test_a_final_output_with_no_kind_counts_as_a_video():
    eq.start_week(MON)
    t = es.create_edit(project()["id"], "old", now=MON)  # e.g. migrated with versions that never got a kind
    it = es.create_iteration(t["id"], "a.mp4", 1, "", now=MON)
    es.complete_iteration(it["id"], "F", now=MON)
    es.update_edit(t["id"], status="final", now=MON)
    assert es.get_edit(t["id"])["kind"] is None
    assert eq.summary(MON)["weeks"][0]["done"] == {"videos": 1, "reels": 0}


def test_week_is_monday_to_sunday():
    assert eq.week_of(MON) == "2026-11-16"
    assert eq.week_of(ts(2026, 11, 22, 23)) == "2026-11-16"
    assert eq.week_of(ts(2026, 11, 23, 0)) == "2026-11-23"
    with pytest.raises(ValueError):
        eq.parse_week("2026-11-17")
    with pytest.raises(ValueError):
        eq.parse_week("nope")


def test_credit_goes_to_the_upload_week_not_the_approval_week():
    eq.start_week(MON)
    p = project()
    deliver(p, "video", uploaded=MON + 6 * D, finalised=MON + 8 * D)  # uploaded Sunday, approved Tuesday
    s = eq.summary(MON + 8 * D)
    last, this = s["weeks"][1], s["weeks"][0]
    assert last["week"] == "2026-11-16" and last["done"]["videos"] == 1
    assert this["done"]["videos"] == 0


def test_videos_count_as_videos_reels_and_clips_together_and_a_week_is_met_missed_or_in_progress():
    eq.start_week(MON)
    p = project()
    for i in range(2):
        deliver(p, "video", MON + D, name=f"v{i}")
    for i in range(8):
        deliver(p, "reel", MON + D, name=f"r{i}")
    deliver(p, "clip", MON + D, name="c0")
    s = eq.summary(MON + 3 * D)
    wk = s["weeks"][0]
    assert wk["done"] == {"videos": 2, "reels": 9} and wk["short"] == {"videos": 0, "reels": 1}
    assert wk["status"] == "in_progress"
    assert eq.summary(MON + 7 * D)["weeks"][1]["status"] == "missed"
    deliver(p, "clip", MON + 2 * D, name="c1")
    assert eq.summary(MON + 7 * D)["weeks"][1]["status"] == "met"


def test_removing_final_or_exempting_takes_the_credit_back():
    eq.start_week(MON)
    p = project()
    t = deliver(p, "video", MON + D)
    assert eq.summary(MON + D)["weeks"][0]["done"]["videos"] == 1
    es.update_edit(t["id"], status="in_review", now=MON + D)
    assert es.get_edit(t["id"])["final_upload_at"] is None
    assert eq.summary(MON + D)["weeks"][0]["done"]["videos"] == 0
    es.update_edit(t["id"], status="final", now=MON + 2 * D)
    es.update_edit(t["id"], quota_exempt=True)
    assert eq.summary(MON + 2 * D)["weeks"][0]["done"]["videos"] == 0


def test_targets_apply_from_their_week_and_excused_is_never_missed():
    eq.start_week(MON)
    eq.set_targets(1, 1, MON + 7 * D)
    s = eq.summary(MON + 14 * D)
    assert [w["target"] for w in s["weeks"]] == [{"videos": 1, "reels": 1}] * 2 + [{"videos": 2, "reels": 10}]
    eq.set_excused("2026-11-16", True, "holiday")
    old = eq.summary(MON + 14 * D)["weeks"][2]
    assert old["status"] == "excused" and old["note"] == "holiday"
    with pytest.raises(ValueError):
        eq.set_targets(-1, 3, MON)


def test_start_week_bounds_the_table_and_cannot_be_in_the_future():
    eq.start_week(MON + 14 * D)
    assert len(eq.summary(MON + 14 * D)["weeks"]) == 1
    eq.set_start_week("2026-11-16", MON + 14 * D)
    assert len(eq.summary(MON + 14 * D)["weeks"]) == 3
    with pytest.raises(ValueError):
        eq.set_start_week("2026-12-07", MON + 14 * D)


def _outbox():
    return [dict(r) for r in db.get().execute("SELECT recipient_role, content, dedupe_key FROM dm_outbox").fetchall()]


@pytest.mark.asyncio
async def test_friday_reminder_once_and_only_while_behind():
    eq.start_week(MON)
    await eq.run(MON + 4 * D + 5 * 3600)  # Friday 17:00: too early
    assert _outbox() == []
    await eq.run(MON + 4 * D + 6 * 3600)  # Friday 18:00
    await eq.run(MON + 5 * D)             # Saturday: same week, no second DM
    rows = [r for r in _outbox() if r["dedupe_key"].startswith("quota_friday")]
    assert len(rows) == 1 and rows[0]["recipient_role"] == "editor"
    assert "2 videos and 10 reels & clips to go, 3 days left" in rows[0]["content"]


@pytest.mark.asyncio
async def test_monday_summary_to_the_streamer():
    eq.start_week(MON)
    p = project()
    deliver(p, "video", MON + D)
    await eq.run(MON + 7 * D - 4 * 3600)  # Monday 08:00 next week: too early
    assert not [r for r in _outbox() if r["dedupe_key"].startswith("quota_monday")]
    await eq.run(MON + 7 * D)
    (row,) = [r for r in _outbox() if r["dedupe_key"].startswith("quota_monday")]
    assert row["recipient_role"] == "streamer"
    assert row["content"] == ("Last week's quota (16 Nov - 22 Nov): videos 1/2, reels & clips 0/10. "
                              "Missed by 1 video and 10 reels & clips.")
    assert eq._to_go({"videos": 0, "reels": 1}) == "1 reel or clip"


@pytest.mark.asyncio
async def test_reminders_can_be_switched_off():
    config._data.update({"editor_quota_friday_reminder": False})
    eq.start_week(MON)
    await eq.run(MON + 5 * D)
    assert _outbox() == []


@pytest.mark.asyncio
async def test_routes_admin_only_writes_both_read(monkeypatch):
    monkeypatch.setattr(eq.time, "time", lambda: MON + D)
    monkeypatch.setattr("editor.editor_api.time.time", lambda: MON + D)
    p = project()
    t = es.create_edit(p["id"], "cut", now=MON)
    async with api() as a:
        status, got = await a.editor("GET", "/api/editor/quota")
        assert status == 200 and got["current_week"] == "2026-11-16"
        for path, body in (("/api/editor/quota/targets", {"videos": 3, "reels": 5}),
                           ("/api/editor/quota/start", {"week": "2026-11-16"}),
                           ("/api/editor/quota/excused", {"week": "2026-11-16", "excused": True})):
            assert (await a.editor("PUT", path, body))[0] == 403
            assert (await a.admin("PUT", path, body))[0] == 200
        assert (await a.editor("PATCH", f"/api/editor/edits/{t['id']}", {"quota_exempt": True}))[0] == 403
        status, got = await a.admin("PATCH", f"/api/editor/edits/{t['id']}", {"quota_exempt": True})
        assert status == 200 and got["quota_exempt"] == 1
        status, got = await a.admin("PUT", "/api/editor/quota/start", {"week": "2026-11-17"})
        assert status == 400 and "Monday" in got["error"]
        status, got = await a.admin("PUT", "/api/editor/quota/targets", {"videos": "2", "reels": 5})
        assert status == 400


def test_existing_finals_are_backfilled_from_their_last_upload(tmp_path):
    db.use_path_for_tests(tmp_path / "old.db")
    conn = db.get()
    conn.executescript(es._DDL)
    conn.execute("INSERT INTO projects(id,name,stream_date,vod_filename,vod_status,created_at,updated_at)"
                 " VALUES(1,'a','d','v','ready',0,0)")
    conn.execute("INSERT INTO edits(id,project_id,name,status,created_at,updated_at) VALUES(1,1,'t','final',0,0)")
    conn.execute("INSERT INTO iterations(edit_id,version,filename,size,status,uploaded_at,created_at,updated_at)"
                 " VALUES(1,1,'a',1,'ready',?,0,0)", (MON + D,))
    assert es.get_edit(1)["final_upload_at"] == MON + D
