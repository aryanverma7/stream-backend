import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import db
import scheduler


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    db.use_path_for_tests(tmp_path / "s.db")
    scheduler._reset_for_tests()
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
