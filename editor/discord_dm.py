"""
Discord REST client: DMs from a bot. No gateway. Never logs the token, user
ids or message content - only the status and Discord's `code`/`message`.
"""
import asyncio
import json

import aiohttp

from core import db
from core.config import config
from core.logger import get_logger

log = get_logger("DiscordDM")

API = "https://discord.com/api/v10"
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_MAX_WAIT_SECONDS = 10
_MAX_CONTENT = 2000


class DiscordNotConfigured(Exception):
    """No bot token."""


class DiscordError(Exception):
    def __init__(self, message: str, status: int = 0, retry_after: "float | None" = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


async def _post(url: str, body: dict, token: str, *, retried: bool = False) -> "tuple[int, dict]":
    """One POST; returns (status, JSON dict). Waits out a short 429 once."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            async with session.post(url, json=body, headers={"Authorization": f"Bot {token}"}) as resp:
                raw = await resp.read()
                status = resp.status
    except (asyncio.TimeoutError, aiohttp.ClientError) as e:
        raise DiscordError(f"Could not reach Discord: {type(e).__name__}", 0) from e
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if status == 429:
        try:
            wait = float(data.get("retry_after", 1))
        except (TypeError, ValueError):
            wait = 1.0
        if not retried and wait <= _MAX_WAIT_SECONDS:
            await asyncio.sleep(wait)
            return await _post(url, body, token, retried=True)
        raise DiscordError("Discord rate limit", 429, wait)
    if status >= 400 and status != 404:
        log.error(f"Discord {status} code={data.get('code')} {data.get('message', '')}")
    return status, data


def _fail(status: int, data: dict) -> DiscordError:
    return DiscordError(f"Discord refused ({status}): {data.get('message', '')}", status)


async def _create_channel(user_id: str, token: str) -> str:
    status, data = await _post(f"{API}/users/@me/channels", {"recipient_id": user_id}, token)
    if status >= 400 or not data.get("id"):
        raise _fail(status, data)
    db.meta_set(f"discord_dm_channel_{user_id}", str(data["id"]))
    return str(data["id"])


async def send_dm(user_id: str, content: str) -> str:
    token = config.get("discord_bot_token", "")
    if not token:
        raise DiscordNotConfigured("No Discord bot token configured.")
    if len(content) > _MAX_CONTENT:
        content = content[:_MAX_CONTENT - 1] + "…"
    key = f"discord_dm_channel_{user_id}"
    channel = db.meta_get(key)
    cached = bool(channel)
    if not channel:
        channel = await _create_channel(user_id, token)
    status, data = await _post(f"{API}/channels/{channel}/messages", {"content": content}, token)
    if status == 404 and cached:
        db.meta_delete(key)
        channel = await _create_channel(user_id, token)
        status, data = await _post(f"{API}/channels/{channel}/messages", {"content": content}, token)
    if status >= 400 or not data.get("id"):
        raise _fail(status, data)
    return str(data["id"])
