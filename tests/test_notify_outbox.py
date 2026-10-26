import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import db
import discord_dm
import notify_outbox as ob
from config import config


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    ob._logged_roles.clear()
    monkeypatch.setattr(config, "_data", {"discord_notifications_enabled": True,
                                          "editor_discord_user_id": "E1", "streamer_discord_user_id": "S1"})
    sent = []

    async def ok(uid, content):
        sent.append((uid, content))
        return "m"
    monkeypatch.setattr(discord_dm, "send_dm", ok)
    return sent


def row():
    return db.get().execute("SELECT * FROM dm_outbox").fetchone()


def test_enqueue_is_idempotent_by_key():
    assert ob.enqueue("editor", "hi", "k", now=1) is True
    assert ob.enqueue("editor", "hi", "k", now=2) is False
    assert db.get().execute("SELECT COUNT(*) FROM dm_outbox").fetchone()[0] == 1


def test_outbox_dedupes_and_survives_restart(tmp_path):
    ob.enqueue("editor", "hi", "k", now=1)
    db.use_path_for_tests(tmp_path / "t.db")
    assert ob.enqueue("editor", "hi", "k", now=2) is False
    assert db.get().execute("SELECT COUNT(*) FROM dm_outbox").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_sends_and_marks_sent(setup):
    ob.enqueue("streamer", "yo", "k", now=1)
    await ob.deliver_due(5)
    assert setup == [("S1", "yo")]
    assert row()["status"] == "sent" and row()["sent_at"] == 5
    assert ob.status()["last_sent_at"] == 5
    await ob.deliver_due(6)
    assert len(setup) == 1


@pytest.mark.asyncio
async def test_disabled_sends_nothing(setup, monkeypatch):
    monkeypatch.setitem(config._data, "discord_notifications_enabled", False)
    assert ob.enqueue("editor", "hi", "k", now=1) is True
    assert (row()["status"], row()["last_error"]) == ("dropped", "notifications disabled")
    await ob.deliver_due(5)
    assert setup == [] and ob.status()["enabled"] is False
    monkeypatch.setitem(config._data, "discord_notifications_enabled", True)
    assert ob.enqueue("editor", "hi", "k", now=9) is False  # key kept: no stale replay
    await ob.deliver_due(10)
    assert setup == []


def test_enqueue_joins_callers_transaction():
    ob.status()  # ensures the DDL; ensure() refuses to run inside a transaction
    with db.transaction():
        ob.enqueue("editor", "a", "k1", now=1)
    with pytest.raises(ZeroDivisionError):
        with db.transaction():
            ob.enqueue("editor", "b", "k2", now=1)
            1 / 0
    assert [r["dedupe_key"] for r in db.get().execute("SELECT * FROM dm_outbox")] == ["k1"]


@pytest.mark.asyncio
async def test_concurrent_deliver_sends_each_once(monkeypatch):
    import asyncio
    sent = []

    async def slow(uid, content):
        await asyncio.sleep(0)
        sent.append(content)
        return "m"
    monkeypatch.setattr(discord_dm, "send_dm", slow)
    for i in range(3):
        ob.enqueue("editor", str(i), f"k{i}", now=1)
    await asyncio.gather(ob.deliver_due(5), ob.deliver_due(5))
    assert sorted(sent) == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_unexpected_exception_goes_back_to_pending(monkeypatch):
    async def bad(uid, content):
        raise ValueError("secret detail")
    monkeypatch.setattr(discord_dm, "send_dm", bad)
    ob.enqueue("editor", "hi", "k", now=1)
    await ob.deliver_due(100)
    r = row()
    assert (r["status"], r["attempts"], r["last_error"]) == ("pending", 1, "ValueError")
    assert ob.status()["sending"] == 0


@pytest.mark.asyncio
async def test_crash_while_sending_replays_once(setup, tmp_path):
    ob.enqueue("editor", "hi", "k", now=1)
    db.get().execute("UPDATE dm_outbox SET status='sending'")
    db.use_path_for_tests(tmp_path / "t.db")
    assert ob.reset_stuck_sending() == 1
    await ob.deliver_due(5)
    assert len(setup) == 1
    db.use_path_for_tests(tmp_path / "t.db")
    assert ob.reset_stuck_sending() == 0
    await ob.deliver_due(50)
    assert len(setup) == 1


@pytest.mark.asyncio
async def test_failure_backs_off_and_keeps_pending(monkeypatch):
    async def bad(uid, content):
        raise discord_dm.DiscordError("boom", 500)
    monkeypatch.setattr(discord_dm, "send_dm", bad)
    ob.enqueue("editor", "hi", "k", now=1)
    await ob.deliver_due(100)
    r = row()
    assert (r["status"], r["attempts"], r["next_attempt_at"], r["last_error"]) == ("pending", 1, 130, "boom")
    assert ob.status()["last_error"] == "boom" and ob.status()["pending"] == 1
    await ob.deliver_due(110)  # not due yet
    assert row()["attempts"] == 1


@pytest.mark.asyncio
async def test_success_clears_last_error(monkeypatch):
    async def bad(uid, content):
        raise discord_dm.DiscordNotConfigured("no token")
    monkeypatch.setattr(discord_dm, "send_dm", bad)
    ob.enqueue("editor", "hi", "k", now=1)
    await ob.deliver_due(100)
    assert ob.status()["last_error"]
    async def ok(uid, content):
        return "m"
    monkeypatch.setattr(discord_dm, "send_dm", ok)
    await ob.deliver_due(1000)
    assert ob.status()["last_error"] is None


@pytest.mark.asyncio
async def test_missing_recipient_id_drops_with_reason(setup, monkeypatch):
    monkeypatch.setitem(config._data, "editor_discord_user_id", "")
    ob.enqueue("editor", "hi", "k", now=1)
    await ob.deliver_due(5)
    assert setup == []
    assert (row()["status"], row()["last_error"]) == ("dropped", "no Discord id for editor")
    assert ob.status()["dropped"] == 1


def test_stuck_sending_reset_at_startup():
    ob.enqueue("editor", "hi", "k", now=1)
    db.get().execute("UPDATE dm_outbox SET status='sending'")
    assert ob.reset_stuck_sending() == 1
    assert row()["status"] == "pending"


@pytest.mark.asyncio
async def test_retry_after_is_respected(monkeypatch):
    async def bad(uid, content):
        raise discord_dm.DiscordError("limited", 429, 900.0)
    monkeypatch.setattr(discord_dm, "send_dm", bad)
    ob.enqueue("editor", "hi", "k", now=1)
    await ob.deliver_due(100)
    assert row()["next_attempt_at"] == 1000


@pytest.mark.asyncio
async def test_at_most_20_oldest_first(setup):
    for i in range(25):
        ob.enqueue("editor", str(i), f"k{i}", now=1)
    await ob.deliver_due(5)
    assert [c for _, c in setup] == [str(i) for i in range(20)]


@pytest.mark.asyncio
async def test_switching_off_drops_rows_already_pending(setup, monkeypatch):
    assert ob.enqueue("editor", "hi", "k", now=1) is True
    monkeypatch.setitem(config._data, "discord_notifications_enabled", False)
    await ob.deliver_due(5)
    assert (row()["status"], row()["last_error"]) == ("dropped", "notifications disabled")
    monkeypatch.setitem(config._data, "discord_notifications_enabled", True)
    await ob.deliver_due(10)
    assert setup == []
