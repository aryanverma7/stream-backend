"""
Points ledger: the balance viewers actually have.

There is one implementation, `points_cloudbot`, and this module is the
seam in front of it - the locks that serialise spends and grants, the
exceptions the rest of the backend catches, and one place for every
feature that charges points to call.

**Why there is only one.** Streamlabs Cloudbot owns the wallet viewers see
when they type !points and accrue into by watching, and Cloudbot has no
API - so `points_cloudbot.py` reaches it by posting !addpoints /
!removepoints through Streamer.bot and reading Cloudbot's replies back out
of the chat stream. That is as indirect as it sounds and it is not a
stopgap; it is the only route that exists.

Two other backends used to live here and both are deleted:

  "api"   - Streamlabs' Loyalty Points REST API, waited on for months
            behind a manual approval. When the approval landed it turned
            out to address a DIFFERENT store: a REST read of the whole
            channel returned `total: 0` while the Cloudbot dashboard
            showed every real viewer, and a write for a username that
            existed nowhere came back `"platform": "points"` - not
            "twitch", not "youtube" - visible over REST and absent from
            Cloudbot's list. Sending `platform` explicitly does not help;
            the field is an output, not an input. Two stores, no bridge
            between them, so the REST ledger held nothing anyone earned
            and nothing !points would ever report.
  "local" - a flat JSON file, for testing the spend paths offline.
            Genuinely useful for that and removed anyway, because a second
            ledger that reads zero for a viewer holding thousands is a
            trap the moment anyone forgets which one is live.

Both are recoverable from git if the situation changes. What must NOT come
back is the belief that "api" and "cloudbot" are one wallet reached two
ways - that was assumed for months, was wrong, and cost a day.
"""
import asyncio

import points_cloudbot
from logger import get_logger

log = get_logger("Points")

class UnknownUser(Exception):
    """
    The live ledger has no record of this user at all - distinct from
    having no points, and distinct from the ledger being unreachable.

    Raised here rather than passed through, so callers can tell a viewer
    something useful without importing points_cloudbot and catching its
    own CloudbotUserNotFound.
    """


_grant_lock = asyncio.Lock()

# The same protection for spends. Cloudbot's spend never reads before it
# writes, so strictly this is not the interleaving hazard _grant_lock
# exists for - but two roulette triggers a frame apart would otherwise
# have two !removepoints in flight at once, and the reply-matching in
# points_cloudbot has no way to tell whose confirmation is whose.
_spend_lock = asyncio.Lock()


# ---------- Public API ----------

async def get_user_points(username: str, platform: str = "") -> int:
    """
    Read a specific user's current balance. Raises if it can't be read,
    which is the NORMAL case for anyone whose balance isn't already
    cached: Cloudbot's `!points` ignores its argument and only ever
    answers about the account that typed it, so there is no way to ask
    about someone else. Nothing that charges points may depend on this -
    use try_spend(), which needs no read.
    """
    return await points_cloudbot.get_user_points(username, platform)


async def subtract_points(username: str, amount: int, platform: str = "") -> None:
    """
    Decrement, without the grant lock: this is not a read-modify-write, so
    it has nothing to interleave (see _grant_lock's comment).
    """
    return await points_cloudbot.subtract_points(username, amount, platform)


async def try_spend(username: str, amount: int, platform: str = "") -> "tuple[bool, int | None]":
    """
    Spend `amount` if the viewer can afford it. The primitive every
    points-charging feature should use.

    Returns (True, None) on success, or (False, balance) when the viewer
    was short, where `balance` is what they actually had - None when that
    cannot be told, which on YouTube is most of the time. Raises only when
    the ledger could not be reached at all, which callers must treat as
    "not paid".

    `platform` is the chat the viewer spoke in, and it is load-bearing:
    Cloudbot keeps a separate wallet per platform and can only resolve a
    username in the chat the command was typed in, so the !removepoints
    has to go to the VIEWER's own chat rather than to one configured one.
    Getting this wrong sent every YouTube spend to Twitch, where those
    handles do not exist.

    This exists instead of get_user_points-then-subtract_points because
    that pair is a check followed by a separate write, and Cloudbot cannot
    support the check at all - it cannot read a viewer's balance, so
    affordability is decided by spending and seeing how much Cloudbot took
    (points_cloudbot.try_spend). Folding both steps into one call is what
    lets the ledger answer the question the only way it can.
    """
    async with _spend_lock:
        try:
            return await points_cloudbot.try_spend(username, amount, platform)
        except points_cloudbot.CloudbotUserNotFound as e:
            raise UnknownUser(str(e)) from e


async def grant_points(username: str, amount: int, platform: str = "") -> "int | None":
    """
    The shared "add points to this user" function - used by BOTH the
    Streamlabs Tips Socket API listener (Task #6, real donations) and the
    admin dashboard's points testing tool (Task #4, manual testing), per
    Section 7/14's design: testing through the dashboard exercises the
    SAME code path as the real thing, not a separate simulation of it.

    Returns the new balance, or None when the grant was confirmed but the
    total cannot be reported - which is usually the case, since Cloudbot's
    confirmation carries the amount added and there is no way to read a
    total back. Callers must render that as "granted", not as a balance of
    zero.
    """
    async with _grant_lock:
        return await points_cloudbot.grant_points(username, amount, platform)
