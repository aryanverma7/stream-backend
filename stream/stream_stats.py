"""
Viewer counts and the follower goal for the overlays on tag "stats"
(viewer-count and follower-goal in the widgets repo).

Every INTERVAL_SECONDS while one of them is connected, one payload:
  {"type": "stream_stats",
   "twitch":  {"viewers": n, "followers": n},
   "youtube": {"viewers": n, "subscribers": n},
   "goal":    {"title": str, "target": n}}
Any number is null when it could not be read (not connected, no key, an
error), so a widget can tell "0 watching" from "unknown". Like the break
screen it is re-sent every time, so a reloaded Browser Source is back within
one interval, and like the Spotify poller nothing is asked while no overlay
is connected. The goal is set on the admin Settings page (Follower goal).
"""
import asyncio

from core.config import config
from core.logger import get_logger
from stream import twitch_client, youtube_client
from stream.widget_hub import widget_hub

log = get_logger("StreamStats")

INTERVAL_SECONDS = 30
TAG = "stats"

_task: "asyncio.Task | None" = None


async def _try(fn, *args):
    try:
        return True, await fn(*args)
    except Exception as e:
        log.warning(f"{fn.__name__} failed: {e}")
        return False, None


def goal() -> dict:
    try:
        target = max(0, int(config.get("follower_goal_target", 0) or 0))
    except (TypeError, ValueError):
        target = 0
    return {"title": str(config.get("follower_goal_title", "") or ""), "target": target}


async def snapshot() -> dict:
    channel = str(config.get("twitch_channel", "") or "")
    twitch_viewers = followers = None
    if channel:
        ok, info = await _try(twitch_client.stream_info, channel)
        twitch_viewers = int((info or {}).get("viewer_count", 0)) if ok else None
        _, followers = await _try(twitch_client.follower_total, channel)
    _, yt_viewers = await _try(youtube_client.live_viewers)
    _, subscribers = await _try(youtube_client.subscriber_count)
    return {
        "type": "stream_stats",
        "twitch": {"viewers": twitch_viewers, "followers": followers},
        "youtube": {"viewers": yt_viewers, "subscribers": subscribers},
        "goal": goal(),
    }


async def tick() -> None:
    if widget_hub.connected_count(TAG) == 0:
        return
    await widget_hub.broadcast(await snapshot(), tag=TAG)


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception:
            log.exception("Stats broadcast failed")
        await asyncio.sleep(INTERVAL_SECONDS)


async def start() -> None:
    """Safe to call twice."""
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_loop())


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        _task = None
