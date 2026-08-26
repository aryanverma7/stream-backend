"""
Twitch app access token (client credentials flow) + live status check.

This is an APP-level token, not a user OAuth token - no user login
involved anywhere in this flow, since "is this channel live" is public
data. Confirmed in the spec (Section 6) as the reason this needs no OAuth
consent screen, unlike the GitHub/Streamlabs flows elsewhere in this
project.
"""
import time

import aiohttp

from config import config
from logger import get_logger

log = get_logger("TwitchClient")

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
STREAMS_URL = "https://api.twitch.tv/helix/streams"

_cached_token: str | None = None
_cached_token_expiry: float = 0


async def get_app_token() -> str:
    global _cached_token, _cached_token_expiry

    if _cached_token and time.time() < _cached_token_expiry:
        return _cached_token

    client_id = config.get("twitch_client_id", "")
    client_secret = config.get("twitch_client_secret", "")

    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, params={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        }) as resp:
            resp.raise_for_status()
            data = await resp.json()

    _cached_token = data["access_token"]
    _cached_token_expiry = time.time() + data["expires_in"] - 300
    log.info("Fetched new Twitch app access token")
    return _cached_token


async def is_channel_live(channel_name: str) -> bool:
    token = await get_app_token()
    client_id = config.get("twitch_client_id", "")

    async with aiohttp.ClientSession() as session:
        async with session.get(
            STREAMS_URL,
            params={"user_login": channel_name},
            headers={"Authorization": f"Bearer {token}", "Client-Id": client_id},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()

    is_live = len(data.get("data", [])) > 0
    log.info(f"Twitch live check for {channel_name}: {is_live}")
    return is_live
