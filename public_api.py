"""
Public-facing routes - deliberately kept SEPARATE from dashboard_api.py.
Everything in dashboard_api.py is meant to be behind the GitHub OAuth gate;
everything here is meant to be open to any visitor, so keeping them in
different files makes it obvious at a glance which routes need which
treatment, rather than having to check each route's registration one by one.

Covers:
  - GET /api/public/clips - lists locally-uploaded highlight clips
  - GET /clips/{filename} - serves the actual video file bytes

Clips are manually dropped into the `clips/` folder by the streamer (per
their own stated workflow - "I'll upload them to the Mac Mini sometime
later") - no Twitch API integration, no automated pulling. This endpoint
just reflects whatever's actually sitting in that folder.
"""
from pathlib import Path

import aiohttp
from aiohttp import web

from config import config
from logger import get_logger
from twitch_client import is_channel_live

log = get_logger("PublicAPI")

CLIPS_DIR = Path(__file__).parent / "clips"
CLIPS_DIR.mkdir(exist_ok=True)

ALLOWED_EXTENSIONS = {".mp4", ".webm", ".mov"}


async def list_clips(request: web.Request) -> web.Response:
    clips = []
    for file in sorted(CLIPS_DIR.iterdir()):
        if file.suffix.lower() in ALLOWED_EXTENSIONS:
            clips.append({
                "filename": file.name,
                "url": f"/clips/{file.name}",
                "title": file.stem.replace("_", " ").replace("-", " "),
            })
    return web.json_response({"clips": clips})


async def live_status(request: web.Request) -> web.Response:
    # BUG FIX: this previously read "streamlabs_channel" - a Streamlabs-
    # specific key meant for the Loyalty Points REST calls, not Twitch.
    # That meant this always sent an empty user_login to Twitch's API
    # unless the two channel names happened to be manually kept in sync,
    # which they weren't - a copy-paste mix-up between two platforms'
    # config keys, not a missing value on the user's end.
    channel = config.get("twitch_channel", "")
    try:
        live = await is_channel_live(channel)
        return web.json_response({"live": live})
    except Exception as e:
        log.warning(f"Live status check failed: {e}")
        return web.json_response({"error": str(e)}, status=502)


async def list_videos(request: web.Request) -> web.Response:
    video_ids = config.get("youtube_video_ids", [])
    api_key = config.get("youtube_api_key", "")

    if not video_ids:
        return web.json_response({"videos": []})

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet", "id": ",".join(video_ids), "key": api_key},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as e:
        log.warning(f"YouTube video fetch failed: {e}")
        return web.json_response({"error": str(e)}, status=502)

    videos = [
        {
            "id": item["id"],
            "title": item["snippet"]["title"],
            "thumbnail": item["snippet"]["thumbnails"]["high"]["url"],
            "url": f"https://www.youtube.com/watch?v={item['id']}",
        }
        for item in data.get("items", [])
    ]
    return web.json_response({"videos": videos})


async def site_config(request: web.Request) -> web.Response:
    return web.json_response(config.public_safe())


def register_public_routes(app: web.Application):
    app.router.add_get("/api/public/clips", list_clips)
    app.router.add_get("/api/public/live-status", live_status)
    app.router.add_get("/api/public/videos", list_videos)
    app.router.add_get("/api/public/site-config", site_config)
    app.router.add_static("/clips", CLIPS_DIR, show_index=False)
