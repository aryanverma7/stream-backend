"""
Twitch app access token (client credentials flow) + live status check.

This is an APP-level token, not a user OAuth token - no user login
involved anywhere in this flow, since "is this channel live" is public
data. Confirmed in the spec (Section 6) as the reason this needs no OAuth
consent screen, unlike the GitHub/Streamlabs flows elsewhere in this
project.
"""
import asyncio
import time

import aiohttp

from core.config import config
from core.logger import get_logger

log = get_logger("TwitchClient")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"
USERS_URL = "https://api.twitch.tv/helix/users"
CHANNELS_URL = "https://api.twitch.tv/helix/channels"
FOLLOWERS_URL = "https://api.twitch.tv/helix/channels/followers"

TIMEOUT = aiohttp.ClientTimeout(total=15)


class TwitchError(Exception):
    """A non-2xx answer. Carries the status only: aiohttp's own error would carry the request URL."""

    def __init__(self, what: str, status: int):
        super().__init__(f"{what}: HTTP {status}")
        self.status = status


_cached_token: str | None = None
_cached_token_expiry: float = 0


async def get_app_token() -> str:
    global _cached_token, _cached_token_expiry

    if _cached_token and time.time() < _cached_token_expiry:
        return _cached_token

    client_id = config.get("twitch_client_id", "")
    client_secret = config.get("twitch_client_secret", "")

    # Form body, never query params: a URL ends up in exception text, logs and error replies.
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }) as resp:
            if resp.status >= 400:
                raise TwitchError("Twitch token request failed", resp.status)
            data = await resp.json()

    _cached_token = data["access_token"]
    _cached_token_expiry = time.time() + data["expires_in"] - 300
    log.info("Fetched new Twitch app access token")
    return _cached_token


async def stream_info(channel_name: str) -> "dict | None":
    """
    The live stream's own record, or None when the channel is offline.

    `is_channel_live` used to be the whole of this and threw away everything
    but the boolean. The title, the game and the stream id are in the same
    response for the same cost, and the go-live announcer needs all three -
    the id in particular, because it is the only thing that distinguishes
    "still the same broadcast" from "a second one tonight".
    """
    token = await get_app_token()
    client_id = config.get("twitch_client_id", "")

    global _cached_token
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(
            STREAMS_URL,
            params={"user_login": channel_name},
            headers={"Authorization": f"Bearer {token}", "Client-Id": client_id},
        ) as resp:
            if resp.status == 401:
                _cached_token = None  # revoked or rotated secret: fetch a fresh one next time
            if resp.status >= 400:
                raise TwitchError("Twitch live check failed", resp.status)
            data = await resp.json()

    streams = data.get("data", []) or []
    info = streams[0] if streams else None
    log.info(f"Twitch live check for {channel_name}: {bool(info)}")
    return info


async def is_channel_live(channel_name: str) -> bool:
    return (await stream_info(channel_name)) is not None


_user_ids: dict[str, str] = {}


async def _helix(session, url: str, params: dict, user_token: "str | None" = None) -> dict:
    """One Helix GET with the app token, or with `user_token` when given."""
    global _cached_token, _user_token
    token = user_token or await get_app_token()
    headers = {"Authorization": f"Bearer {token}", "Client-Id": config.get("twitch_client_id", "")}
    async with session.get(url, params=params, headers=headers) as resp:
        if resp.status == 401:
            if user_token:
                _user_token = None
            else:
                _cached_token = None
        if resp.status >= 400:
            raise TwitchError("Twitch channel lookup failed", resp.status)
        return await resp.json()


async def _helix_data(session, url: str, params: dict) -> list:
    return (await _helix(session, url, params)).get("data", []) or []


async def _user_id(session, login: str) -> "str | None":
    if login not in _user_ids:
        users = await _helix_data(session, USERS_URL, {"login": login})
        if not users:
            return None
        _user_ids[login] = users[0]["id"]
    return _user_ids[login]


async def channel_title(channel_name: str) -> "str | None":
    """
    The channel's current stream title, live or not (unlike stream_info, which
    only has it while live): the streamer sets it before going live, and the
    starting-soon screen shows it. None when the channel does not exist.
    """
    login = channel_name.strip().lower()
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        uid = await _user_id(session, login)
        if uid is None:
            return None
        channels = await _helix_data(session, CHANNELS_URL, {"broadcaster_id": uid})
    return (channels[0].get("title") or None) if channels else None


# ---------- the streamer's own user token (Connect Twitch, twitch_oauth.py) ----------
# Only the follower total needs it: Get Channel Followers refuses app tokens.
# The durable credential is twitch_refresh_token; the access token is memory-only.

_user_token: "str | None" = None
_user_token_expiry: float = 0
_user_lock = asyncio.Lock()


def forget_user_token() -> None:
    global _user_token
    _user_token = None


async def user_token() -> "str | None":
    """A fresh user access token, or None when Twitch was never connected."""
    global _user_token, _user_token_expiry
    refresh = config.get("twitch_refresh_token", "")
    if not refresh:
        return None
    async with _user_lock:
        if _user_token and time.time() < _user_token_expiry:
            return _user_token
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.post(TOKEN_URL, data={
                "client_id": config.get("twitch_client_id", ""),
                "client_secret": config.get("twitch_client_secret", ""),
                "grant_type": "refresh_token",
                "refresh_token": refresh,
            }) as resp:
                if resp.status >= 400:
                    raise TwitchError("Twitch user token refresh failed", resp.status)
                data = await resp.json()
        _user_token = data["access_token"]
        _user_token_expiry = time.time() + int(data.get("expires_in", 3600)) - 300
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != refresh:
            config.set("twitch_refresh_token", new_refresh)
            config.save()
        return _user_token


async def follower_total(channel_name: str) -> "int | None":
    """The channel's follower count, or None while Twitch is not connected."""
    token = await user_token()
    if not token:
        return None
    login = channel_name.strip().lower()
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        uid = await _user_id(session, login)
        if uid is None:
            return None
        data = await _helix(session, FOLLOWERS_URL, {"broadcaster_id": uid, "first": 1}, user_token=token)
    return int(data.get("total", 0))
