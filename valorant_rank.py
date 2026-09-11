"""
`!rank` - the streamer's current rank and RR, in chat.

The rank overlay already reads this from HenrikDev's unofficial Valorant
API, and this is the same call from the backend so a viewer watching on a
phone, or with the overlay cropped out of their view, can still ask.

**The credentials move to config.json here.** `rank_session_tracker.html`
carries its Henrik key in plaintext in a file that gets copied to the OBS
machine by hand; this module reads `henrik_api_key` instead, so the one
place a secret has to live is the file that already holds every other
secret. The widget is unchanged - fixing that is a separate job, and
duplicating the key was never the part worth fixing first.

**Cached, because chat asks in bursts.** One person asking prompts three
more, and HenrikDev is a free community API with its own rate limits that
this project has no claim on. A 60-second cache means a room full of
people asking at once costs one request - and a rank cannot change in
under a match anyway, so nothing is lost.

**A failure answers rather than going quiet.** An unreachable API, a
missing key and a mistyped Riot ID are three different fixes, and a
command that silently does nothing looks identical to a bot that is down.
"""
import time

import aiohttp

from config import config
from logger import get_logger

log = get_logger("Rank")

API_BASE = "https://api.henrikdev.xyz/valorant"

DEFAULT_CACHE_SECONDS = 60
DEFAULT_TIMEOUT_SECONDS = 8

_cached: "dict | None" = None
_cached_at: float = 0.0


class RankUnavailable(Exception):
    """Could not be read. Its own type so the chat reply can say which kind of problem it is."""


def _cache_seconds() -> float:
    try:
        return float(config.get("rank_cache_seconds", DEFAULT_CACHE_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_CACHE_SECONDS)


def is_configured() -> bool:
    return bool(
        config.get("henrik_api_key", "")
        and config.get("riot_name", "")
        and config.get("riot_tag", "")
    )


def forget_cache() -> None:
    """Exists for the tests."""
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


def describe(rank: dict) -> str:
    """
    "Diamond 2 - 47 RR". The tier name already carries the number, so
    nothing here re-derives it from the tier id.
    """
    tier = rank.get("tier") or "Unranked"
    rr = rank.get("rr")
    return f"{tier} - {rr} RR" if rr is not None else str(tier)


async def _fetch() -> dict:
    region = config.get("riot_region", "ap")
    platform = config.get("riot_platform", "pc")
    name = config.get("riot_name", "")
    tag = config.get("riot_tag", "")
    url = f"{API_BASE}/v3/mmr/{region}/{platform}/{name}/{tag}"

    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"Authorization": config.get("henrik_api_key", "")}) as resp:
                if resp.status == 404:
                    raise RankUnavailable(
                        "that Riot ID doesn't exist - check riot_name and riot_tag in config"
                    )
                if resp.status in (401, 403):
                    raise RankUnavailable("the Henrik API key was refused")
                if resp.status == 429:
                    raise RankUnavailable("rank lookups are being rate-limited right now")
                if resp.status != 200:
                    raise RankUnavailable(f"the rank API answered {resp.status}")
                body = await resp.json()
    except RankUnavailable:
        raise
    except Exception as e:
        raise RankUnavailable(f"couldn't reach the rank API ({e})") from e

    current = (body.get("data") or {}).get("current") or {}
    tier = (current.get("tier") or {}).get("name")
    if not tier:
        raise RankUnavailable("the rank API answered but had no rank in it")
    return {"tier": tier, "rr": current.get("rr")}


async def current_rank() -> dict:
    """The cached rank, fetching it if the cache is cold or stale."""
    global _cached, _cached_at

    if not is_configured():
        raise RankUnavailable("rank lookups aren't set up - set henrik_api_key, riot_name and riot_tag")

    if _cached is not None and (time.time() - _cached_at) < _cache_seconds():
        return _cached

    rank = await _fetch()
    _cached = rank
    _cached_at = time.time()
    return rank


async def handle_chat_command(event: dict) -> None:
    """Registered via streamerbot.on_event() from main.py, like the other feature handlers."""
    from streamerbot_client import parse_chat_message, streamerbot

    chat = parse_chat_message(event)
    if chat is None:
        return
    text = (chat["text"] or "").strip()
    username = chat["username"]
    if not text.startswith("!") or not username:
        return
    if text[1:].split()[0].lower() not in ("rank", "rr", "elo"):
        return

    try:
        rank = await current_rank()
        reply = f"@{username} {config.get('riot_name', 'currently')} is {describe(rank)}"
    except RankUnavailable as e:
        # Said out loud rather than swallowed: a command that does nothing
        # is indistinguishable from a bot that is down, and these three
        # failures have three different fixes.
        log.warning(f"!rank failed: {e}")
        reply = f"@{username} can't read the rank right now - {e}"

    if config.get("roulette_chat_replies_enabled", True):
        await streamerbot.send_chat_message(reply, platform=chat["platform"] or "twitch")
