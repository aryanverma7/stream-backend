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

  * Lookups are scoped to the platform the command is typed on, AND to
    that platform's own users. Streamlabs' dashboard shows one Loyalty
    list holding both platforms, which is display-only - it is not one
    addressable pool. Established by test:
      - `!addpoints pinkuthagoat 500` in TWITCH chat: works.
      - `!addpoints pinkukumarchinkiwala4849 10` (a YouTube row) in
        TWITCH chat: "Unable to find pinkukumarchinkiwala4849."
    So a command must be typed in the viewer's OWN chat. Everything here
    is keyed on (platform, username) for that reason, and _await_reply()
    sends to the platform the key names rather than to one configured
    one. That single global was a real bug: every YouTube spend went to
    Twitch chat, where the handle does not exist, and the silence that
    came back was mistaken for Cloudbot refusing to serve YouTube at all.

  * YouTube executes mod commands and says NOTHING on success.
    `!addpoints <name> 1000` there moved a balance from 560 to 1560 with
    no reply at all, while the identical command on Twitch answers "<mod>
    has successfully added ...". Failures still reply on YouTube ("Unable
    to find <name>."), and that asymmetry is the only reason this is
    workable: on such a platform silence IS the success signal, so
    _await_write() waits only long enough to catch a rejection.

    What that costs, stated rather than discovered: Cloudbot still clamps
    on YouTube, and nothing reports it. A viewer short of the cost is
    charged whatever they held and gets the thing anyway - the exact
    failure try_spend() was built to prevent on Twitch, and here there is
    no signal to prevent it with. A balance already in the cache is
    checked first, which catches the obvious cases for free; beyond that
    the loss is bounded by what the viewer had. Refusing every YouTube
    spend instead was the alternative, and it is worse.

    `cloudbot_silent_write_platforms` lists these; a confirmation that
    does arrive inside the grace window is used normally, so a platform
    that starts answering needs no config change.

  * Cloudbot lowercases the target and strips ONE leading "@" before
    looking it up. `!addpoints @DualBladeX 10` answered "successfully
    added 10 Bunds to dualbladex" - it matched the Twitch login, not the
    YouTube row literally named "@DualBladeX", and `!addpoints
    @@DualBladeX` then failed on "@dualbladex". Two YouTube rows are
    stored with a leading "@", which no input can address.

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
# How long to wait for a REJECTION on a platform that doesn't confirm
# writes. Nothing arriving means it worked, so this is dead time on the
# happy path and wants to be short - but it sits inside an 18-second
# voting window, so it must not be so short that a slow "Unable to find"
# is mistaken for success.
DEFAULT_SILENT_WRITE_GRACE_SECONDS = 1.5
# Platforms where !addpoints/!removepoints take effect but say nothing.
# YouTube is the observed case: `!addpoints <name> 1000` moved a balance
# from 560 to 1560 with no reply at all, while the same command in Twitch
# chat answers "<mod> has successfully added ...". Failures still reply
# on YouTube ("Unable to find <name>."), which is the only reason this
# can work at all.
DEFAULT_SILENT_WRITE_PLATFORMS = ("youtube",)

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

# Every key below is (platform, username), both lowercased. Cloudbot
# keeps a separate wallet per platform, so the same handle on Twitch and
# on YouTube is two different people as far as points are concerned - and
# a reply arriving in one chat must never resolve a lookup made in the
# other.
_cache: "dict[tuple, tuple[float, int]]" = {}
_pending_reads: "dict[tuple, list[asyncio.Future]]" = {}
_pending_writes: "dict[tuple, list[asyncio.Future]]" = {}


def _key(platform: str, username: str) -> tuple:
    return ((platform or "").lower(), (username or "").lower())

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


def _lock_for(key: tuple) -> asyncio.Lock:
    if key not in _locks:
        _locks[key] = asyncio.Lock()
    return _locks[key]


def default_platform() -> str:
    """
    Where a command goes when the caller doesn't say. Only donations and
    the dashboard's manual tools reach that state - a chat command always
    knows which chat it came from.
    """
    return config.get("cloudbot_platform", "twitch")


def writes_are_confirmed(platform: str) -> bool:
    """
    Whether Cloudbot answers a successful write in this chat.

    Twitch confirms and reports the amount actually taken, which is what
    lets try_spend() see a clamp. YouTube says nothing on success, so
    there the amount is unknowable and only a rejection is visible.
    """
    silent = config.get("cloudbot_silent_write_platforms", DEFAULT_SILENT_WRITE_PLATFORMS)
    return (platform or "").lower() not in {str(p).lower() for p in silent}


def _silent_write_grace() -> float:
    return float(
        config.get("cloudbot_silent_write_grace_seconds", DEFAULT_SILENT_WRITE_GRACE_SECONDS)
    )


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

def _resolve(waiters: dict, key: tuple, value) -> None:
    for future in waiters.pop(key, []):
        if not future.done():
            future.set_result(value)


def _resolve_error(waiters: dict, key: tuple, error: Exception) -> None:
    for future in waiters.pop(key, []):
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

    # The chat this reply arrived in decides which wallet it is about.
    # Cloudbot answers in the same chat the command was typed in, and its
    # wallets are per platform, so a Twitch reply must not resolve a
    # YouTube lookup that happens to name the same handle.
    platform = chat.get("platform", "")

    balance = parse_balance_reply(text)
    if balance is not None:
        username, points = balance
        key = _key(platform, username)
        _cache[key] = (time.monotonic(), points)
        _resolve(_pending_reads, key, points)
        return

    write = parse_write_reply(text)
    if write is not None:
        username, points, direction = write
        _resolve(_pending_writes, _key(platform, username), (points, direction))
        return

    # Answered, and answered "no". Without this the caller sits out the
    # full reply timeout waiting for a confirmation that is never coming -
    # six seconds inside an eighteen-second voting window.
    missing = parse_not_found_reply(text)
    if missing is not None:
        error = CloudbotUserNotFound(missing)
        key = _key(platform, missing)
        _resolve_error(_pending_writes, key, error)
        _resolve_error(_pending_reads, key, error)


async def _await_reply(waiters: dict, key: tuple, command: str):
    """
    Sends `command` in the chat `key`'s platform names, and waits for
    Cloudbot to answer about that user there. Raises on timeout - never
    returns a guess.

    The command goes to the VIEWER's own chat, not to one configured
    platform. Cloudbot only resolves a username within the chat the
    command was typed in, so a YouTube viewer's spend sent to Twitch chat
    could never work no matter what Cloudbot's YouTube support does.
    """
    platform = key[0] or default_platform()
    future: asyncio.Future = asyncio.get_running_loop().create_future()
    waiters.setdefault(key, []).append(future)

    if not await streamerbot.send_chat_message(command, platform=platform):
        waiters.get(key, []).remove(future)
        raise RuntimeError("Not connected to Streamer.bot - cannot reach Cloudbot")

    try:
        return await asyncio.wait_for(future, timeout=_timeout())
    except asyncio.TimeoutError:
        # Named causes rather than one guess. The only suggestion here
        # used to be "is the bot account a moderator?", and it sent a real
        # investigation the wrong way twice - the actual cause that time
        # was this module's own caller blocking the socket read loop, so
        # Cloudbot's reply was sitting unread rather than unsent.
        raise TimeoutError(
            f"Cloudbot did not answer {command!r} within {_timeout()}s - its command "
            f"cooldown may have swallowed this one, the account we speak as may not be a "
            f"moderator, or nothing may be reading the socket."
        )
    finally:
        remaining = waiters.get(key, [])
        if future in remaining:
            remaining.remove(future)
        if not remaining:
            waiters.pop(key, None)


async def _await_write(key: tuple, command: str):
    """
    Sends a write and returns Cloudbot's confirmation - (points,
    direction) - or None on a platform that doesn't send one.

    On a silent platform this waits only long enough to catch a
    REJECTION. Nothing arriving is the success signal there, which is
    weak but is the only signal offered: `!addpoints <name> 1000` in
    YouTube chat moved a balance from 560 to 1560 and said nothing, while
    `!addpoints <unknown> 1` there still answers "Unable to find <name>."

    A confirmation that does turn up inside the grace window is returned
    like any other, so a platform that starts answering is handled
    correctly without a config change.
    """
    if writes_are_confirmed(key[0]):
        return await _await_reply(_pending_writes, key, command)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending_writes.setdefault(key, []).append(future)
    try:
        if not await streamerbot.send_chat_message(command, platform=key[0] or default_platform()):
            raise RuntimeError("Not connected to Streamer.bot - cannot reach Cloudbot")
        try:
            return await asyncio.wait_for(future, timeout=_silent_write_grace())
        except asyncio.TimeoutError:
            # No rejection inside the window, so it went through.
            return None
    finally:
        remaining = _pending_writes.get(key, [])
        if future in remaining:
            remaining.remove(future)
        if not remaining:
            _pending_writes.pop(key, None)


# ---------- The operations points.py dispatches to ----------

async def get_user_points(username: str, platform: str = "", use_cache: bool = True) -> int:
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
    key = _key(platform or default_platform(), username)
    if use_cache:
        cached = _cache.get(key)
        if cached is not None and time.monotonic() - cached[0] < _cache_ttl():
            return cached[1]
    raise CloudbotReadUnavailable(
        f"Cloudbot cannot report {username}'s balance on demand - !points only ever "
        f"answers about the account that typed it. Their balance becomes known once "
        f"they use !points themselves, or once the roulette charges them."
    )


async def try_spend(username: str, amount: int, platform: str = "") -> "tuple[bool, int | None]":
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
    key = _key(platform or default_platform(), username)
    async with _lock_for(key):
        # On a silent platform a clamp is invisible, so a balance we
        # already hold is the only chance to refuse a viewer who plainly
        # cannot pay - and it costs no chat line. Skipped where the
        # confirmation will tell us the truth anyway.
        if not writes_are_confirmed(key[0]):
            cached = _cache.get(key)
            if cached is not None and time.monotonic() - cached[0] < _cache_ttl():
                if cached[1] < amount:
                    log.info(
                        f"{username} has {cached[1]} points and needs {amount} - refusing "
                        f"without spending, since {key[0]} would not report the shortfall"
                    )
                    return False, cached[1]

        confirmation = await _await_write(key, f"!removepoints {username} {amount}")

        if confirmation is None:
            # Unconfirmed. Cloudbot still clamps here, and nothing reports
            # it, so a viewer short of the cost is charged whatever they
            # had and gets the thing anyway. Deliberately accepted: the
            # alternative is refusing every spend on this platform, and
            # the loss is bounded by what they held. See the module
            # docstring's note on silent platforms.
            cached = _cache.get(key)
            if cached is not None:
                _cache[key] = (cached[0], max(cached[1] - amount, 0))
            log.info(
                f"Spent {amount} points from {username} via Cloudbot on {key[0]} "
                f"(unconfirmed - this platform does not answer writes)"
            )
            return True, None

        removed, _ = confirmation
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
                await _await_write(key, f"!addpoints {username} {removed}")
            except Exception:
                log.error(
                    f"REFUND FAILED: took {removed} points from {username} for a spend "
                    f"they could not afford, and could not give them back. Restore by "
                    f"hand with: !addpoints {username} {removed}"
                )
                raise
        return False, removed


async def subtract_points(username: str, amount: int, platform: str = "") -> None:
    """
    Spends points unconditionally, and waits for Cloudbot to confirm.

    Kept for the dispatcher's own subtract_points, which promises nothing
    about affordability. Callers that need "only if they can afford it"
    want try_spend instead - Cloudbot clamps, so this quietly takes
    whatever is there when the balance is short.
    """
    key = _key(platform or default_platform(), username)
    async with _lock_for(key):
        await _await_write(key, f"!removepoints {username} {amount}")
        cached = _cache.get(key)
        if cached is not None:
            _cache[key] = (cached[0], max(cached[1] - amount, 0))
        log.info(f"Removed {amount} points from {username} via Cloudbot")


async def grant_points(username: str, amount: int, platform: str = "") -> "int | None":
    """
    Grants points. Returns the new balance, or None when it isn't knowable.

    Cloudbot's confirmation reports the amount added, not the new total,
    and there is no way to read a total back (see get_user_points). So the
    total is only returned when this module already had a cached balance
    to add to; otherwise the grant is confirmed and the total is None,
    which is the honest answer rather than a plausible one.
    """
    key = _key(platform or default_platform(), username)
    async with _lock_for(key):
        await _await_write(key, f"!addpoints {username} {amount}")
        cached = _cache.get(key)
        if cached is None:
            log.info(f"Added {amount} points to {username} via Cloudbot - new total unknown")
            return None
        total = cached[1] + amount
        _cache[key] = (time.monotonic(), total)
        log.info(f"Added {amount} points to {username} via Cloudbot - new balance {total}")
        return total
