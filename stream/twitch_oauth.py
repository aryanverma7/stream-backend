"""
Connect Twitch - the one-time login that gives the backend the streamer's own
user token, for the one thing an app token cannot read: the follower total
(Get Channel Followers requires a user token). Same shape as spotify_oauth.py:
admin-only (not in OPEN_PATHS), state checked once, refresh token saved to
config.json as twitch_refresh_token.

The redirect URI is <public_base_url>/auth/twitch/callback and must be added,
exactly, to the OAuth Redirect URLs of the Twitch app (dev.twitch.tv/console)
whose client id/secret are already in config. moderator:read:followers is the
documented scope; without it Twitch still returns the total, but asking for it
keeps the call inside what the docs promise.
"""
import secrets
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

from core.config import config
from core.logger import get_logger
from stream import twitch_client

log = get_logger("TwitchAuth")

AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
SCOPES = "moderator:read:followers"

_pending_states: set = set()


def redirect_uri() -> str:
    base = str(config.get("public_base_url", "") or "").rstrip("/")
    return f"{base}/auth/twitch/callback" if base else ""


async def twitch_login(request: web.Request) -> web.Response:
    client_id = config.get("twitch_client_id", "")
    if not client_id or not redirect_uri():
        return web.json_response(
            {"error": "twitch_client_id and public_base_url must be set in config.json first"}, status=400)
    state = secrets.token_urlsafe(24)
    _pending_states.add(state)
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(),
        "scope": SCOPES,
        "state": state,
        "force_verify": "true",  # always show the consent screen, so the right account is picked
    }
    raise web.HTTPFound(f"{AUTHORIZE_URL}?{urlencode(params)}")


async def twitch_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error:
        log.error(f"Twitch authorization declined or failed: {error}")
        return web.json_response({"error": f"Twitch authorization failed: {error}"}, status=400)
    code = request.query.get("code")
    state = request.query.get("state")
    if not code:
        return web.json_response({"error": "Missing code parameter"}, status=400)
    if not state or state not in _pending_states:
        log.warning("Twitch callback with an unrecognized or reused state - rejecting")
        return web.json_response({"error": "Invalid or expired state parameter"}, status=400)
    _pending_states.discard(state)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.post(twitch_client.TOKEN_URL, data={
            "client_id": config.get("twitch_client_id", ""),
            "client_secret": config.get("twitch_client_secret", ""),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        }) as resp:
            data = await resp.json()
            if resp.status != 200 or "refresh_token" not in data:
                log.error(f"Twitch token exchange failed: HTTP {resp.status}")
                return web.json_response({"error": data.get("message") or "Token exchange failed"}, status=400)

    config.set("twitch_refresh_token", data["refresh_token"])
    config.save()
    twitch_client.forget_user_token()
    log.info("Twitch connected - the follower goal can read the follower count")
    raise web.HTTPFound("/admin")
