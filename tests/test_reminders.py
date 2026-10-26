import calendar
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import db
import deletions
import editor_store as es
import reminders
from config import config

H = 3600


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    db.use_path_for_tests(tmp_path / "t.db")
    monkeypatch.setattr(config, "_data", {"discord_notifications_enabled": True,
                                          "editor_discord_user_id": "E1"})
    yield
    monkeypatch.undo()
    time.tzset()


def at(hour, day=1):
    return calendar.timegm((2026, 1, day, hour, 0, 0))


def project(clicked, kind_s=None):
    p = es.create_project("Stream A", "2026-01-01", "t", "a.mp4", 1, now=0, duration_s=kind_s)
    es.mark_vod_ready(p["id"], "f", now=0)
    es.record_download_click(p["id"], now=clicked)
    return p


def count():
    return db.get().execute("SELECT COUNT(*) FROM dm_outbox WHERE recipient_role='editor'").fetchone()[0]


@pytest.mark.asyncio
async def test_no_reminder_before_three_hours():
    project(at(10))
    await reminders.run(at(13) - 1)
    assert count() == 0
    await reminders.run(at(13))
    assert count() == 1


@pytest.mark.asyncio
async def test_hourly_inside_window():
    project(at(9))
    for h in (12, 13, 14):
        await reminders.run(at(h))
        await reminders.run(at(h) + 600)  # same hour: nothing more
    assert count() == 3
    row = db.get().execute("SELECT content FROM dm_outbox").fetchone()[0]
    assert "Stream A" in row and "VOD" not in row  # wording from the brief, name interpolated


@pytest.mark.asyncio
async def test_quiet_outside_window_then_one_at_noon():
    project(at(17, 1))
    for h in (22, 23):
        await reminders.run(at(h, 1))
    for h in (0, 5, 11):
        await reminders.run(at(h, 2))
    assert count() == 0
    await reminders.run(at(12, 2))
    assert count() == 1


@pytest.mark.asyncio
async def test_outage_sends_one_catch_up_not_a_burst():
    project(at(9))
    await reminders.run(at(12))
    await reminders.run(at(18))
    assert count() == 2
    await reminders.run(at(18) + 60)
    assert count() == 2


@pytest.mark.asyncio
async def test_stops_once_delete_requested():
    p = project(at(9))
    await reminders.run(at(12))
    assert db.meta_get(f"reminder_next_{p['id']}") is not None
    deletions.request_delete("project", p["id"], "Stream A", username="a", role="admin", now=at(12))
    await reminders.run(at(13))
    assert count() == 1
    assert db.meta_get(f"reminder_next_{p['id']}") is None


@pytest.mark.asyncio
async def test_clip_wording_and_no_click_no_reminder():
    p = es.create_project("C", "2026-01-01", "t", "c.mp4", 1, now=0, duration_s=10)
    es.mark_vod_ready(p["id"], "f", now=0)
    await reminders.run(at(15))
    assert count() == 0  # no download click (admin clicks never set it: see test_editor_api)
    es.record_download_click(p["id"], now=at(9))
    await reminders.run(at(15))
    assert "clip" in db.get().execute("SELECT content FROM dm_outbox").fetchone()[0]


@pytest.mark.asyncio
async def test_recovery_after_discord_outage_delivers_one_reminder(monkeypatch):
    import discord_dm
    import notify_outbox
    sent = []

    async def fail(uid, text):
        raise discord_dm.DiscordError("down")

    async def ok(uid, text):
        sent.append(text)
    project(at(9))
    monkeypatch.setattr(discord_dm, "send_dm", fail)
    for h in range(12, 18):
        await reminders.run(at(h))
        await notify_outbox.deliver_due(at(h))
    assert sent == []
    monkeypatch.setattr(discord_dm, "send_dm", ok)
    await reminders.run(at(18))
    await notify_outbox.deliver_due(at(18) + 1)
    assert len(sent) == 1
