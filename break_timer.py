"""
The "be right back" screen's countdown.

The streamer sets a duration on the admin dashboard *before* switching to
the break scene, and the overlay counts it down. That ordering is the
whole design: the dashboard is on a second monitor and the break scene is
about to be the only thing on stream, so the number has to be set while
there is still something else to look at.

State lives here rather than in config.json. A break is not a setting -
it is something that is happening right now, it must not survive a
backend restart as though it were still running, and writing it to disk
would mean a disk write per break for a value that is meaningless an hour
later. `started_at` plus `duration_seconds` is the whole of it; the
remaining time is derived, never stored, so nothing can drift.

**The overlay is told, not asked.** Like every other widget here it holds
no credential and cannot call an admin route, so the state is broadcast on
tag "break". It is re-broadcast on a timer as well as on change, for the
same reason spotify's now-playing is: a Browser Source can be reloaded at
any moment, and a change-only stream would leave a freshly-loaded break
screen blank until the next time somebody touched the dashboard - which,
during a break, is nobody.

Running past zero is not an error and is not hidden. The overlay says
"back any moment" and keeps showing the screen, because the alternative -
hiding it, or letting the number go negative - would either put the
streamer on camera before they are there or start a stopwatch of how late
they are.
"""
import asyncio
import time

from aiohttp import web

from config import config
from logger import get_logger
from widget_hub import widget_hub

log = get_logger("BreakTimer")

DEFAULT_MINUTES = 10
DEFAULT_MESSAGE = "Back in a moment"
# How often the state is re-sent even when nothing has changed. Five
# seconds is far more often than a break changes and still cheap: one
# small JSON object to at most one connected overlay.
BROADCAST_INTERVAL_SECONDS = 5
# A break longer than this is somebody typing into the wrong box. Not a
# hard rule about breaks - it is a guard against `600` being read as
# minutes when it was meant as seconds.
MAX_MINUTES = 180

_active: bool = False
_started_at: "float | None" = None
_duration_seconds: int = 0
_message: str = ""

_broadcast_task: "asyncio.Task | None" = None


def default_minutes() -> int:
    try:
        return max(1, int(config.get("break_default_minutes", DEFAULT_MINUTES)))
    except (TypeError, ValueError):
        return DEFAULT_MINUTES


def status() -> dict:
    """
    The current break, with the remaining time derived rather than stored.

    `remaining_seconds` floors at zero and `overdue` says whether it got
    there, so the overlay never has to render a negative number and never
    has to decide on its own what running out means.
    """
    remaining = 0
    if _active and _started_at is not None:
        remaining = max(0, int(round(_started_at + _duration_seconds - time.time())))
    return {
        "active": _active,
        "duration_seconds": _duration_seconds,
        "remaining_seconds": remaining,
        "overdue": bool(_active and remaining == 0),
        "message": _message,
        "default_minutes": default_minutes(),
    }


async def _broadcast() -> None:
    await widget_hub.broadcast({"type": "break_state", **status()}, tag="break")


async def start(minutes: float, message: str = "") -> dict:
    """Starts (or restarts) a break. Restarting is how "give me five more" works."""
    global _active, _started_at, _duration_seconds, _message

    minutes = max(0.0, min(float(minutes), MAX_MINUTES))
    _active = True
    _started_at = time.time()
    _duration_seconds = int(round(minutes * 60))
    _message = (message or "").strip() or DEFAULT_MESSAGE

    # Remembered as the next default, because the same streamer takes
    # roughly the same break twice - and typing it again every time is
    # exactly the friction that stops the screen being used at all.
    config.set("break_default_minutes", max(1, int(round(minutes))) if minutes >= 1 else DEFAULT_MINUTES)
    config.save()

    log.info(f"Break started: {_duration_seconds}s - {_message!r}")
    await _broadcast()
    return status()


async def stop() -> dict:
    global _active, _started_at, _duration_seconds
    if _active:
        log.info("Break ended")
    _active = False
    _started_at = None
    _duration_seconds = 0
    await _broadcast()
    return status()


def reset() -> None:
    """Drops all state without broadcasting. Exists for the tests."""
    global _active, _started_at, _duration_seconds, _message
    _active = False
    _started_at = None
    _duration_seconds = 0
    _message = ""


# ---------- HTTP ----------

async def handle_get(request: web.Request) -> web.Response:
    return web.json_response(status(), headers={"Cache-Control": "no-store"})


async def handle_post(request: web.Request) -> web.Response:
    """
    POST /api/break - {"action": "start", "minutes": 10, "message": "..."}
    or {"action": "stop"}.

    An action verb rather than a PUT of the whole state, because "start"
    means *restart the clock from now*, and a body carrying the same
    duration twice cannot express that.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    action = str(body.get("action", "")).lower()
    if action == "stop":
        return web.json_response(await stop())
    if action != "start":
        return web.json_response({"error": 'action must be "start" or "stop"'}, status=400)

    try:
        minutes = float(body.get("minutes", default_minutes()))
    except (TypeError, ValueError):
        return web.json_response({"error": "minutes must be a number"}, status=400)
    if minutes <= 0:
        return web.json_response({"error": "minutes must be greater than zero"}, status=400)

    return web.json_response(await start(minutes, str(body.get("message", ""))))


async def _broadcast_loop() -> None:
    while True:
        try:
            await _broadcast()
        except Exception:
            log.exception("Break-state broadcast failed")
        await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)


async def start_broadcaster() -> None:
    """Keeps a reloaded break overlay in sync. Safe to call twice."""
    global _broadcast_task
    if _broadcast_task and not _broadcast_task.done():
        return
    _broadcast_task = asyncio.create_task(_broadcast_loop())


async def stop_broadcaster() -> None:
    global _broadcast_task
    if _broadcast_task:
        _broadcast_task.cancel()
        _broadcast_task = None


def register_routes(app: web.Application) -> None:
    app.router.add_get("/api/break", handle_get)
    app.router.add_post("/api/break", handle_post)
