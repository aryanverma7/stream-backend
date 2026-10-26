"""
One background loop that runs every timed job (deletes, backups, reminders).

The Mac Mini loses power often and its clock battery may be weak, so it can
boot with the clock in the past. Jobs compare absolute timestamps, so a clock
behind the last time we recorded would fire nothing, or worse, fire wrong.
tick() therefore refuses to run any job until the clock passes
meta["clock_last_seen"].
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

import db

log = logging.getLogger(__name__)

TICK_SECONDS = 30
_LAST_SEEN_KEY = "clock_last_seen"

_jobs: dict[str, Callable[[float], Awaitable[None]]] = {}
_task: asyncio.Task | None = None
_boot_time: float | None = None
_insane = False


def backoff_seconds(attempts: int) -> float:
    return min(30 * 2**attempts, 3600)


def register(name: str, job: Callable[[float], Awaitable[None]]) -> None:
    """Jobs run in registration order; an existing name is replaced in place."""
    _jobs[name] = job


def clock_is_sane(now: float) -> bool:
    last = db.meta_get(_LAST_SEEN_KEY)
    return last is None or now >= float(last)


def boot_time() -> float:
    """
    When start() was first called in this process. If start() never ran (tests,
    scripts), the first call of boot_time() itself fixes it at time.time().
    """
    global _boot_time
    if _boot_time is None:
        _boot_time = time.time()
    return _boot_time


async def tick(now: float | None = None) -> bool:
    global _insane
    now = time.time() if now is None else now
    if not clock_is_sane(now):
        if not _insane:
            _insane = True
            log.warning("Clock (%s) is behind the last time seen (%s); scheduled jobs paused",
                        now, db.meta_get(_LAST_SEEN_KEY))
        return False
    _insane = False
    for name, job in list(_jobs.items()):
        try:
            await job(now)
        except Exception:
            log.exception("Scheduled job %r failed", name)
    # max() so a job that ran with a clock that moved back is never recorded as earlier.
    prev = db.meta_get(_LAST_SEEN_KEY)
    db.meta_set(_LAST_SEEN_KEY, str(max(float(prev), now) if prev is not None else now))
    return True


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("Scheduler tick failed")
        await asyncio.sleep(TICK_SECONDS)


async def start() -> None:
    global _task
    boot_time()
    if _task is not None:
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


def _reset_for_tests() -> None:
    global _boot_time, _insane
    _jobs.clear()
    _boot_time = None
    _insane = False
