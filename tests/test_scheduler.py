import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import db
from core import scheduler


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "s.db")
    scheduler._reset_for_tests()

    async def no_answer():
        return None
    monkeypatch.setattr(scheduler.net_time, "online_time", no_answer)  # never the network
    yield
    scheduler._reset_for_tests()


def test_backoff_caps_at_an_hour():
    assert scheduler.backoff_seconds(0) == 30
    assert scheduler.backoff_seconds(2) == 120
    assert scheduler.backoff_seconds(20) == 3600


@pytest.mark.asyncio
async def test_clock_behind_last_seen_blocks_jobs():
    ran = []

    async def job(now):
        ran.append(now)

    scheduler.register("j", job)
    assert await scheduler.tick(1000) is True
    assert await scheduler.tick(900) is False
    assert ran == [1000]
    assert await scheduler.tick(1001) is True
    assert ran == [1000, 1001]
    assert float(db.meta_get("clock_last_seen")) == 1001


@pytest.mark.asyncio
async def test_one_failing_job_does_not_stop_others():
    ran = []

    async def bad(now):
        raise RuntimeError("boom")

    async def good(now):
        ran.append(now)

    scheduler.register("bad", bad)
    scheduler.register("good", good)
    assert await scheduler.tick(5) is True
    assert ran == [5]


@pytest.mark.asyncio
async def test_register_same_name_replaces():
    ran = []

    async def a(now):
        ran.append("a")

    async def b(now):
        ran.append("b")

    scheduler.register("x", a)
    scheduler.register("x", b)
    await scheduler.tick(1)
    assert ran == ["b"]


@pytest.mark.asyncio
async def test_last_seen_persists_across_reconnect(tmp_path):
    p = tmp_path / "persist.db"
    db.use_path_for_tests(p)
    await scheduler.tick(2000)
    db.use_path_for_tests(p)
    assert scheduler.clock_is_sane(1999) is False
    assert scheduler.clock_is_sane(2000) is True


@pytest.mark.asyncio
async def test_insane_streak_logs_once(caplog):
    await scheduler.tick(100)
    with caplog.at_level("WARNING"):
        await scheduler.tick(50)
        await scheduler.tick(60)
    assert len([r for r in caplog.records if "clock" in r.message.lower()]) == 1
    await scheduler.tick(200)
    with caplog.at_level("WARNING"):
        await scheduler.tick(10)
    assert len([r for r in caplog.records if "clock" in r.message.lower()]) == 2


@pytest.mark.asyncio
async def test_start_stop_and_boot_time():
    await scheduler.start()
    t = scheduler.boot_time()
    await scheduler.start()
    assert scheduler.boot_time() == t
    await scheduler.stop()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_garbage_meta_value_runs_jobs_and_logs_error(caplog):
    ran = []

    async def job(now):
        ran.append(now)

    scheduler.register("j", job)
    db.meta_set("clock_last_seen", "garbage")
    with caplog.at_level("ERROR"):
        assert await scheduler.tick(5) is True
    assert ran == [5]
    assert any(r.levelname == "ERROR" for r in caplog.records)
    assert float(db.meta_get("clock_last_seen")) == 5


@pytest.mark.asyncio
async def test_now_equal_to_last_is_sane():
    await scheduler.tick(1000)
    assert await scheduler.tick(1000) is True


@pytest.mark.asyncio
async def test_blocked_tick_keeps_last_seen():
    await scheduler.tick(1000)
    await scheduler.tick(900)
    assert float(db.meta_get("clock_last_seen")) == 1000


@pytest.mark.asyncio
async def test_status_shows_paused_since():
    assert scheduler.status() == {"sane": True, "last_seen": None, "paused_since": None,
                                  "last_internet_check": {"at": None, "agreed": None}}
    await scheduler.tick(1000)
    await scheduler.tick(900)
    await scheduler.tick(950)
    s = scheduler.status()
    assert s["paused_since"] == 900 and s["last_seen"] == 1000 and s["sane"] is False
    await scheduler.tick(1001)
    assert scheduler.status()["paused_since"] is None


@pytest.mark.asyncio
async def test_reset_clock_unblocks():
    await scheduler.tick(5000)
    assert await scheduler.tick(100) is False
    scheduler.reset_clock(100)
    assert scheduler.status()["paused_since"] is None
    assert await scheduler.tick(101) is True


@pytest.mark.asyncio
async def test_non_finite_last_seen_is_garbage_and_logged_once(caplog):
    ran = []

    async def job(now):
        ran.append(now)

    scheduler.register("j", job)
    with caplog.at_level("ERROR"):
        for bad in ("nan", "inf", "garbage"):
            db.meta_set("clock_last_seen", bad)
            assert await scheduler.tick(5) is True
    assert ran == [5, 5, 5]
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


# -- internet clock check --

def _online(monkeypatch, value):
    calls = []

    async def fake():
        calls.append(1)
        return value
    monkeypatch.setattr(scheduler.net_time, "online_time", fake)
    return calls


def _mono(monkeypatch, t):
    monkeypatch.setattr(scheduler, "_monotonic", lambda: t[0])


@pytest.mark.asyncio
async def test_internet_agreement_reopens_gate_and_clamps_deletes(monkeypatch):
    from editor import deletions
    db.meta_set("clock_last_seen", "5000000")  # the clock once ran fast
    clamped = []
    scheduler.on_auto_reset(lambda now: clamped.append(deletions.clamp_after_clock_reset(now)))
    ran = []

    async def job(now):
        ran.append(now)
    scheduler.register("j", job)
    _online(monkeypatch, 1000 + 60)
    assert await scheduler.tick(1000) is True
    assert ran == [1000] and clamped == [0]
    assert float(db.meta_get("clock_last_seen")) == 1000
    assert scheduler.status()["last_internet_check"] == {"at": 1000, "agreed": True}


@pytest.mark.asyncio
async def test_internet_disagreement_stays_paused_logged_once(monkeypatch, caplog):
    db.meta_set("clock_last_seen", "5000000")
    t = [0.0]
    _mono(monkeypatch, t)
    _online(monkeypatch, 1000 + 3600)
    with caplog.at_level("WARNING"):
        for i in range(3):
            t[0] += 301
            assert await scheduler.tick(1000 + i) is False
    assert sum("off internet time" in r.getMessage() for r in caplog.records) == 1
    assert scheduler.status()["last_internet_check"] == {"at": 1002, "agreed": False}
    assert float(db.meta_get("clock_last_seen")) == 5000000


@pytest.mark.asyncio
async def test_no_internet_answer_stays_paused():
    db.meta_set("clock_last_seen", "5000000")
    assert await scheduler.tick(1000) is False
    assert scheduler.status()["sane"] is False
    assert scheduler.status()["last_internet_check"] == {"at": 1000, "agreed": None}


@pytest.mark.asyncio
async def test_internet_checks_throttled_to_five_minutes(monkeypatch):
    db.meta_set("clock_last_seen", "5000000")
    t = [0.0]
    _mono(monkeypatch, t)
    calls = _online(monkeypatch, None)
    await scheduler.tick(1000)
    t[0] += 299
    await scheduler.tick(1030)
    assert len(calls) == 1
    t[0] += 1
    await scheduler.tick(1060)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_sane_clock_never_asks_the_internet(monkeypatch):
    calls = _online(monkeypatch, 0)
    assert await scheduler.tick(1000) is True
    assert calls == [] and scheduler.status()["last_internet_check"] == {"at": None, "agreed": None}


@pytest.mark.asyncio
async def test_auto_reset_clamps_before_the_deletions_job_runs(monkeypatch, tmp_path):
    from editor import deletions, editor_store as es
    deletions._reset_for_tests()
    deletions._grace_applied = True  # this process already had a sane tick, then paused
    p = es.create_project("Ranked grind", "2026-10-20", "t", "vod-1020.mp4", 100, now=500)
    es.mark_vod_ready(p["id"], "VODFILE", now=500)
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="aryan", role="admin", now=500)
    db.meta_set("clock_last_seen", "5000000")
    scheduler.on_auto_reset(deletions.clamp_after_clock_reset)
    scheduler.register("deletions", deletions.run_due)
    _online(monkeypatch, 10000)
    assert await scheduler.tick(10000) is True  # its delete_at (4100) is long past under the real clock
    [row] = deletions.pending()
    assert row["id"] == r["request_id"] and row["delete_at"] == 10000 + deletions.DELETE_GRACE_SECONDS
