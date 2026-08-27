"""
Points via Streamlabs Cloudbot, driven through chat.

The third points backend, and the one that keeps viewers' real balances.
Streamlabs gates its Loyalty Points REST API behind a manual approval
(see points.py), but Cloudbot reads and writes that *same* wallet through
ordinary chat commands, and Cloudbot is already in the channel. So this
backend asks it, in chat, the way a moderator would:

    !points <user>            -> "@<user>, you have 19 Bunds."
    !addpoints <user> <n>     -> "<mod> has successfully added 1 Bunds to <user>"
    !removepoints <user> <n>  -> "<mod> has successfully removed 1 Bunds from <user>."

The wording above is what Cloudbot actually replied in this channel, not
a guess from documentation. The currency word is whatever the streamer
named theirs, so nothing here matches on it.

Why this and not the local ledger: Cloudbot's wallet is the one viewers
already earn by watching, on both platforms, and the one `!points` tells
them about. A separate ledger only ever holds what donors and the admin
put in it, and leaves a viewer with two different balances and no way to
know which one the roulette is spending.

WHAT THIS COSTS, stated rather than discovered: every call is a visible
chat message and a visible reply. A balance read on every vote would put
two lines in chat per vote, which during an 18-second window is a wall of
bot spam - so balances are read once and then held (see _cache), with
every spend written through immediately. Writes are never cached, only
reads.

Requirements this backend has that the others don't:
  * The account Streamer.bot speaks as must be a MODERATOR - !addpoints
    and !removepoints are mod-only.
  * streamerbot_send_as_bot must point at an account that can actually
    post, which is the thing to check first if nothing here works.
"""
import asyncio
import re
import time

from config import config
from logger import get_logger
from streamerbot_client import streamerbot

log = get_logger("PointsCloudbot")

DEFAULT_REPLY_TIMEOUT_SECONDS = 6.0
# How long a balance read stays usable. Cloudbot keeps accruing watch-time
# points while this is held, so a stale value is always an UNDER-estimate
# of what the viewer has - which refuses a purchase they could afford,
# rather than granting one they couldn't. That is the cheap side.
DEFAULT_CACHE_TTL_SECONDS = 60.0

# "@dualbladex, you have 19 Bunds."
_POINTS_RE = re.compile(
    r"@?(?P<user>[A-Za-z0-9_]+),?\s+you\s+have\s+(?P<points>[\d,]+)\s+\S+",
    re.IGNORECASE,
)
# "dualbladex has successfully added 1 Bunds to dualbladex"
_ADDED_RE = re.compile(
    r"successfully\s+added\s+(?P<points>[\d,]+)\s+\S+\s+to\s+(?P<user>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)
# "dualbladex has successfully removed 1 Bunds from dualbladex."
_REMOVED_RE = re.compile(
    r"successfully\s+removed\s+(?P<points>[\d,]+)\s+\S+\s+from\s+(?P<user>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)

# username (lowercased) -> (monotonic timestamp, balance)
_cache: "dict[str, tuple[float, int]]" = {}
# username (lowercased) -> futures waiting on a reply about that user
_pending_reads: "dict[str, list[asyncio.Future]]" = {}
_pending_writes: "dict[str, list[asyncio.Future]]" = {}

# Serializes the whole read-decide-write sequence. Cloudbot replies are
# matched by username, not by request, so two overlapping operations on
# the SAME user would race for each other's replies.
_locks: "dict[str, asyncio.Lock]" = {}


def _lock_for(username: str) -> asyncio.Lock:
    key = username.lower()
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def _timeout() -> float:
    return float(config.get("cloudbot_reply_timeout_seconds", DEFAULT_REPLY_TIMEOUT_SECONDS))


def _cache_ttl() -> float:
    return float(config.get("cloudbot_cache_ttl_seconds", DEFAULT_CACHE_TTL_SECONDS))


def reset() -> None:
    """Drops all cached balances and pending waiters. For tests, and for handle_reset."""
    _cache.clear()
    _pending_reads.clear()
    _pending_writes.clear()
    _locks.clear()


# ---------- Reply parsing (pure, so it can be pinned in tests) ----------

def parse_balance_reply(text: str) -> "tuple[str, int] | None":
    """(username, points) from a !points reply, or None if this isn't one."""
    match = _POINTS_RE.search(text)
    if match is None:
        return None
    return match.group("user").lower(), int(match.group("points").replace(",", ""))


def parse_write_reply(text: str) -> "tuple[str, int, str] | None":
    """
    (username, points, "added"|"removed") from an !addpoints/!removepoints
    confirmation, or None.
    """
    match = _ADDED_RE.search(text)
    if match is not None:
        return match.group("user").lower(), int(match.group("points").replace(",", "")), "added"
    match = _REMOVED_RE.search(text)
    if match is not None:
        return match.group("user").lower(), int(match.group("points").replace(",", "")), "removed"
    return None


# ---------- Listening for Cloudbot ----------

def _resolve(waiters: dict, username: str, value) -> None:
    for future in waiters.pop(username, []):
        if not future.done():
            future.set_result(value)


async def handle_chat_event(chat: dict) -> None:
    """
    Registered from main.py against the parsed chat stream. Matches
    Cloudbot's replies and hands them to whoever is waiting.

    Deliberately does NOT check who sent the message. The bot's account
    name is configurable in Streamlabs and differs per channel, so
    matching on it would be one more thing to get wrong silently; the
    reply shapes above are specific enough on their own, and the only
    cost of a false match is a viewer quoting a bot line at the exact
    moment a lookup is in flight.
    """
    text = chat.get("text", "")
    if not text:
        return

    balance = parse_balance_reply(text)
    if balance is not None:
        username, points = balance
        _cache[username] = (time.monotonic(), points)
        _resolve(_pending_reads, username, points)
        return

    write = parse_write_reply(text)
    if write is not None:
        username, points, direction = write
        _resolve(_pending_writes, username, (points, direction))


async def _await_reply(waiters: dict, username: str, command: str, platform: str):
    """
    Sends `command` in chat and waits for Cloudbot to answer about
    `username`. Raises on timeout - never returns a guess.
    """
    key = username.lower()
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    waiters.setdefault(key, []).append(future)

    if not await streamerbot.send_chat_message(command, platform=config.get("cloudbot_platform", "twitch")):
        waiters.get(key, []).remove(future)
        raise RuntimeError("Not connected to Streamer.bot - cannot reach Cloudbot")

    try:
        return await asyncio.wait_for(future, timeout=_timeout())
    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Cloudbot did not answer {command!r} within {_timeout()}s - is the bot account a moderator?"
        )
    finally:
        remaining = waiters.get(key, [])
        if future in remaining:
            remaining.remove(future)
        if not remaining:
            waiters.pop(key, None)


# ---------- The three operations points.py dispatches to ----------

async def get_user_points(username: str, use_cache: bool = True) -> int:
    """
    A viewer's balance, from cache when it is fresh enough.

    The cache exists to keep chat readable, not to be clever: a live read
    on every vote would put two bot lines in chat per vote. A held value
    can only be stale downwards, since Cloudbot keeps accruing while we
    hold it, so the worst case is refusing a purchase the viewer could
    actually afford.
    """
    key = username.lower()
    if use_cache:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _cache_ttl():
            return cached[1]

    async with _lock_for(key):
        return await _await_reply(_pending_reads, key, f"!points {username}", "twitch")


async def subtract_points(username: str, amount: int) -> None:
    """
    Spends points, and waits for Cloudbot to confirm it happened.

    The confirmation is not optional. Cloudbot owns the wallet, so this
    backend cannot know whether a viewer could actually afford the spend -
    and a spend that silently failed would hand out a free roulette. No
    confirmation inside the timeout is therefore treated as a failure,
    which refuses the action rather than granting it unpaid.
    """
    key = username.lower()
    async with _lock_for(key):
        await _await_reply(_pending_writes, key, f"!removepoints {username} {amount}", "twitch")
        cached = _cache.get(key)
        if cached is not None:
            _cache[key] = (cached[0], max(cached[1] - amount, 0))
        log.info(f"Removed {amount} points from {username} via Cloudbot")


async def grant_points(username: str, amount: int) -> int:
    """
    Grants points and returns the new balance.

    Cloudbot's confirmation reports the amount added, not the new total,
    so the total is read back afterwards - bypassing the cache, since the
    whole point is that the number just changed.
    """
    key = username.lower()
    async with _lock_for(key):
        await _await_reply(_pending_writes, key, f"!addpoints {username} {amount}", "twitch")
        _cache.pop(key, None)

    total = await get_user_points(username, use_cache=False)
    log.info(f"Added {amount} points to {username} via Cloudbot - new balance {total}")
    return total
