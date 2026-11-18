"""
The stream title for the starting-soon overlay (tag "title").

The overlay is a local file in OBS with no credential, so the backend asks
Twitch (the YouTube title is set to the same text) and broadcasts it. Like the
break screen it is re-sent on a timer rather than on change, so a Browser Source
reload is back within one interval, and like the Spotify poller it asks nothing
while no overlay is connected. A failed lookup sends nothing: the overlay keeps
showing whatever it had (its own CONFIG text at first).
"""
import asyncio

from core.config import config
from core.logger import get_logger
from stream import twitch_client
from stream.widget_hub import widget_hub

log = get_logger("StreamTitle")

INTERVAL_SECONDS = 30
TAG = "title"

_task: "asyncio.Task | None" = None


async def tick() -> None:
    channel = config.get("twitch_channel", "")
    if not channel or widget_hub.connected_count(TAG) == 0:
        return
    title = await twitch_client.channel_title(channel)
    if title:
        await widget_hub.broadcast({"type": "stream_title", "title": title}, tag=TAG)


async def _loop() -> None:
    while True:
        try:
            await tick()
        except Exception as e:
            log.warning(f"Stream title lookup failed: {e}")
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
