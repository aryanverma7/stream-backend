"""
One background loop that runs every timed job (deletes, backups, reminders).

The Mac Mini loses power often and its clock battery may be weak, so it can
boot with the clock in the past. Jobs compare absolute timestamps, so a clock
behind the last time we recorded would fire nothing, or worse, fire wrong.
tick() therefore refuses to run any job until the clock passes
meta["clock_last_seen"].
"""
import asyncio
import math
import time
from typing import Awaitable, Callable

import db
from logger import get_logger

log = get_logger("Scheduler")

TICK_SECONDS = 30
_LAST_SEEN_KEY = "clock_last_seen"

_jobs: dict[str, Callable[[float], Awaitable[None]]] = {}
_task: asyncio.Task | None = None
_boot_time: float | None = None
_insane = False
_paused_since: float | None = None
_garbage_logged = False


def backoff_seconds(attempts: int) -> float:
    return min(30 * 2**attempts, 3600)


def register(name: str, job: Callable[[float], Awaitable[None]]) -> None:
    """Jobs run in registration order; an existing name is replaced in place."""
    _jobs[name] = job


def _last_seen() -> float | None:
    global _garbage_logged
    raw = db.meta_get(_LAST_SEEN_KEY)
    if raw is None:
        return None
    try:
        v = float(raw)
    except ValueError:
        v = math.nan
    if math.isfinite(v):
        return v
    # Treated as absent; the next sane tick rewrites it. (nan/inf would pause every job forever.)
    if not _garbage_logged:
        _garbage_logged = True
        log.error("Unparseable %s %r; ignoring it", _LAST_SEEN_KEY, raw)
    return None


def last_seen() -> float | None:
    """meta["clock_last_seen"]; read before start() it is when the previous process last ticked."""
    return _last_seen()


def clock_is_sane(now: float) -> bool:
    last = _last_seen()
    return last is None or now >= last


def status() -> dict:
    return {"sane": _paused_since is None, "last_seen": _last_seen(), "paused_since": _paused_since}


def reset_clock(now: float | None = None) -> None:
    """Admin escape hatch: trust the current clock (e.g. after it once jumped forward)."""
    global _insane, _paused_since
    db.meta_set(_LAST_SEEN_KEY, str(time.time() if now is None else now))
    _insane = False
    _paused_since = None


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
    global _insane, _paused_since
    now = time.time() if now is None else now
    if not clock_is_sane(now):
        if not _insane:
            _insane = True
            _paused_since = now
            log.warning("Clock (%s) is behind the last time seen (%s); scheduled jobs paused",
                        now, _last_seen())
        return False
    _insane = False
    _paused_since = None
    for name, job in list(_jobs.items()):
        try:
            await job(now)
        except Exception:
            log.exception("Scheduled job %r failed", name)
    # max() so a job that ran with a clock that moved back is never recorded as earlier.
    prev = _last_seen()
    db.meta_set(_LAST_SEEN_KEY, str(now if prev is None else max(prev, now)))
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
    global _boot_time, _insane, _paused_since, _garbage_logged
    _jobs.clear()
    _garbage_logged = False
    _boot_time = None
    _insane = False
    _paused_since = None
