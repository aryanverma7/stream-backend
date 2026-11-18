"""
YouTube Data API v3 reads for the stats overlays: the channel's subscriber
count and the live broadcast's concurrent viewers.

Needs youtube_api_key (a server key, NOT the website-restricted Drive key) and,
for subscribers, youtube_channel_id. The key goes in the X-goog-api-key header,
never the URL. Quota: each call costs 1 unit of the free 10,000 a day.

YouTube's public subscriber count is rounded down to three significant figures
(documented), so a goal fed by it moves in steps.
"""
import time

import aiohttp

from core.config import config
from core.logger import get_logger
from stream import discord_live

log = get_logger("YouTube")

API = "https://www.googleapis.com/youtube/v3"
TIMEOUT = aiohttp.ClientTimeout(total=10)
# /live is a ~1 MB page: remember what it said instead of fetching it every tick.
VIDEO_ID_TTL_SECONDS = 300
NO_VIDEO_TTL_SECONDS = 60

_video_id: tuple = (0.0, "")


class YouTubeError(Exception):
    pass


def _key() -> str:
    return str(config.get("youtube_api_key", "") or "").strip()


async def _get(path: str, params: dict) -> list:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(f"{API}/{path}", params=params, headers={"X-goog-api-key": _key()}) as resp:
            if resp.status >= 400:
                raise YouTubeError(f"YouTube {path} failed: HTTP {resp.status}")
            return (await resp.json()).get("items", []) or []


async def subscriber_count() -> "int | None":
    channel = str(config.get("youtube_channel_id", "") or "").strip()
    if not _key() or not channel:
        return None
    items = await _get("channels", {"part": "statistics", "id": channel})
    stats = items[0].get("statistics", {}) if items else {}
    if not stats or stats.get("hiddenSubscriberCount"):
        return None
    return int(stats.get("subscriberCount", 0))


async def _live_video_id(now: float) -> str:
    global _video_id
    at, vid = _video_id
    if now - at < (VIDEO_ID_TTL_SECONDS if vid else NO_VIDEO_TTL_SECONDS):
        return vid
    vid = await discord_live.youtube_live_video_id()
    _video_id = (now, vid)
    return vid


async def live_viewers(now: "float | None" = None) -> "int | None":
    """Concurrent viewers on the live broadcast; 0 when not live; None without a key."""
    if not _key():
        return None
    vid = await _live_video_id(time.time() if now is None else now)
    if not vid:
        return 0
    items = await _get("videos", {"part": "liveStreamingDetails", "id": vid})
    details = items[0].get("liveStreamingDetails", {}) if items else {}
    # Absent when the broadcast is scheduled or over.
    return int(details.get("concurrentViewers", 0) or 0)
