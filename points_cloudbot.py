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

THREE CLOUDBOT BEHAVIOURS CONFIRMED BY TEST IN THIS CHANNEL, all three
load-bearing, none of them documented anywhere:

  * `!points <user>` IGNORES its argument. It always answers about the
    account that typed it. dualbladex typing `!points pinkuthagoat` got
    "@dualbladex, you have 961 Bunds." while pinkuthagoat actually held
    1760. There is therefore no way to look up a viewer's balance, and
    this module does not try - get_user_points() serves the cache or
    raises. A read that quietly returns the bot's balance is worse than
    no read.

  * `!removepoints` CLAMPS to the balance, and its confirmation reports
    the amount ACTUALLY removed. `!removepoints pinkuthagoat 99999`
    against 1760 answered "successfully removed 1760 Bunds from
    pinkuthagoat." That single fact is what makes this backend viable
    without a read: try_spend() spends first, and a short spend both
    identifies itself and reveals the exact balance. See try_spend().

  * Cloudbot's user database is PER PLATFORM. `!addpoints pinkuthagoat`
    works in Twitch chat and answers "Unable to find pinkuthagoat." in
    YouTube chat, and no YouTube name works there either. Commands go to
    `cloudbot_platform` (default "twitch"), so the points economy is
    Twitch-only - a YouTube-only viewer has a separate Cloudbot wallet
    that nothing here can address.

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

# "Unable to find pinkudagoat." - Cloudbot's answer when the named user
# is not in its database for the platform the command was typed on. It
# only ever says this for !addpoints/!removepoints; !points answers an
# unknown name with the CALLER's balance instead (see get_user_points).
_NOT_FOUND_RE = re.compile(
    r"unable\s+to\s+find\s+@?(?P<user>[A-Za-z0-9_]+)",
    re.IGNORECASE,
)

# username (lowercased) -> (monotonic timestamp, balance)
_cache: "dict[str, tuple[float, int]]" = {}
# username (lowercased) -> futures waiting on a reply about that user
_pending_reads: "dict[str, list[asyncio.Future]]" = {}
_pending_writes: "dict[str, list[asyncio.Future]]" = {}

class CloudbotUserNotFound(Exception):
    """
    Cloudbot has never seen this user on the platform we asked on.

    Distinct from a timeout, and it fails differently: a read treats it as
    a zero balance (accurate - there is no wallet), while a write raises,
    because a grant or a spend that landed nowhere must not be reported
    as having happened.
    """


class CloudbotReadUnavailable(Exception):
    """
    A balance was asked for that Cloudbot has no way to report.

    Not a transport failure - Cloudbot is answering fine, it just cannot
    be asked about anybody but the account typing the command. Raised
    rather than guessed so a caller shows an error instead of a number
    that belongs to the bot.
    """


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


def parse_not_found_reply(text: str) -> "str | None":
    """The username Cloudbot could not find, or None if this isn't that reply."""
    match = _NOT_FOUND_RE.search(text)
    if match is None:
        return None
    return match.group("user").lower()


# ---------- Listening for Cloudbot ----------

def _resolve(waiters: dict, username: str, value) -> None:
    for future in waiters.pop(username, []):
        if not future.done():
            future.set_result(value)


def _resolve_error(waiters: dict, username: str, error: Exception) -> None:
    for future in waiters.pop(username, []):
        if not future.done():
            future.set_exception(error)


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
        return

    # Answered, and answered "no". Without this the caller sits out the
    # full reply timeout waiting for a confirmation that is never coming -
    # six seconds inside an eighteen-second voting window.
    missing = parse_not_found_reply(text)
    if missing is not None:
        error = CloudbotUserNotFound(missing)
        _resolve_error(_pending_writes, missing, error)
        _resolve_error(_pending_reads, missing, error)


async def _await_reply(waiters: dict, username: str, command: str):
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


# ---------- The operations points.py dispatches to ----------

async def get_user_points(username: str, use_cache: bool = True) -> int:
    """
    A viewer's balance - only if one is already cached.

    This backend CANNOT look up an arbitrary user. `!points <name>` ignores
    its argument and answers about whoever typed it: dualbladex typing
    `!points pinkuthagoat` got back "@dualbladex, you have 961 Bunds."
    while pinkuthagoat's own balance was 1760. Sending it anyway would
    cost a chat line and return a confidently wrong number, so it is not
    sent at all.

    The cache is still worth reading, because it fills up on its own:
    every `!points` a viewer types about themselves goes past
    handle_chat_event, and every write this module makes updates the entry
    it just changed. A hit can only be stale downwards - Cloudbot keeps
    accruing while it is held - so it refuses purchases rather than
    granting them.

    Affordability is NOT decided here. try_spend() does it without a read,
    by spending and reading how much Cloudbot actually took.
    """
    key = username.lower()
    if use_cache:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _cache_ttl():
            return cached[1]
    raise CloudbotReadUnavailable(
        f"Cloudbot cannot report {username}'s balance on demand - !points only ever "
        f"answers about the account that typed it. Their balance becomes known once "
        f"they use !points themselves, or once the roulette charges them."
    )


async def try_spend(username: str, amount: int) -> "tuple[bool, int | None]":
    """
    Spends `amount`, and reports whether it could be paid in full.

    Returns (True, None) when it was, or (False, balance) when it was not,
    where `balance` is what the viewer actually had. Raises only when
    Cloudbot could not be reached or did not answer.

    This is a spend-first design, and it exists because there is no read
    to check against first (see get_user_points). What makes it work is
    that Cloudbot CLAMPS: `!removepoints pinkuthagoat 99999` against a
    balance of 1760 answered "successfully removed 1760 Bunds from
    pinkuthagoat." - it takes what is there, and the confirmation reports
    the amount actually taken. So a short spend identifies itself, and the
    number it reports is the viewer's balance.

    A short spend is then refunded with the exact amount taken. There is a
    window - roughly one chat round trip - where the viewer is at zero,
    which is the price of not having a read. If the refund itself fails
    the viewer is genuinely down those points, so it is logged at error
    level with the amount, which is the one case here a human has to fix
    by hand.
    """
    key = username.lower()
    async with _lock_for(key):
        removed, _ = await _await_reply(
            _pending_writes, key, f"!removepoints {username} {amount}"
        )
        if removed >= amount:
            cached = _cache.get(key)
            if cached is not None:
                _cache[key] = (cached[0], max(cached[1] - amount, 0))
            log.info(f"Spent {amount} points from {username} via Cloudbot")
            return True, None

        # Short. `removed` is what they had, so we now know their balance
        # exactly - cache it before giving it back.
        _cache[key] = (time.monotonic(), removed)
        log.info(
            f"{username} could not afford {amount} points (had {removed}) - refunding"
        )
        if removed > 0:
            try:
                await _await_reply(
                    _pending_writes, key, f"!addpoints {username} {removed}"
                )
            except Exception:
                log.error(
                    f"REFUND FAILED: took {removed} points from {username} for a spend "
                    f"they could not afford, and could not give them back. Restore by "
                    f"hand with: !addpoints {username} {removed}"
                )
                raise
        return False, removed


async def subtract_points(username: str, amount: int) -> None:
    """
    Spends points unconditionally, and waits for Cloudbot to confirm.

    Kept for the dispatcher's own subtract_points, which promises nothing
    about affordability. Callers that need "only if they can afford it"
    want try_spend instead - Cloudbot clamps, so this quietly takes
    whatever is there when the balance is short.
    """
    key = username.lower()
    async with _lock_for(key):
        await _await_reply(_pending_writes, key, f"!removepoints {username} {amount}")
        cached = _cache.get(key)
        if cached is not None:
            _cache[key] = (cached[0], max(cached[1] - amount, 0))
        log.info(f"Removed {amount} points from {username} via Cloudbot")


async def grant_points(username: str, amount: int) -> "int | None":
    """
    Grants points. Returns the new balance, or None when it isn't knowable.

    Cloudbot's confirmation reports the amount added, not the new total,
    and there is no way to read a total back (see get_user_points). So the
    total is only returned when this module already had a cached balance
    to add to; otherwise the grant is confirmed and the total is None,
    which is the honest answer rather than a plausible one.
    """
    key = username.lower()
    async with _lock_for(key):
        await _await_reply(_pending_writes, key, f"!addpoints {username} {amount}")
        cached = _cache.get(key)
        if cached is None:
            log.info(f"Added {amount} points to {username} via Cloudbot - new total unknown")
            return None
        total = cached[1] + amount
        _cache[key] = (time.monotonic(), total)
        log.info(f"Added {amount} points to {username} via Cloudbot - new balance {total}")
        return total
