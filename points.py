"""
Points ledger, with two interchangeable backends behind one API.

`points_backend` in config.json selects between them:

  "api"    (default) - Streamlabs' Loyalty Points REST API. The real
                       thing: the same balance viewers see when they type
                       !points in chat, accruing on its own with watch
                       time.
  "cloudbot"         - the SAME wallet as "api", reached by asking
                       Streamlabs Cloudbot in chat instead of over REST
                       (points_cloudbot.py). Needs no approval, because
                       Cloudbot is already in the channel.
  "local"            - a flat JSON file on this machine (points_local.py).
                       Offline testing only: it holds nothing a viewer
                       earned by watching, so `!points` and the roulette
                       disagree about what anyone has.

The switch exists because Streamlabs gates the Loyalty Points API behind
a manual approval step that is separate from OAuth scopes entirely. A
token issued with points.read and points.write, for the app owner's own
channel, still answers every call with:

    401 "Access to Loyalty points API, requires special approval. Please
    request for loyalty access from third party app (OAuth Clients)
    dashboard. We will review and get back to you."

That approval is requested from the Streamlabs developer dashboard and
granted on their schedule, not ours. Rather than leave the roulette
untestable until it lands, the local backend stands in - see
points_local.py's own docstring for what it does and does not give you.
Flipping back is a config edit and takes effect immediately; no code
here changes.

---

The Streamlabs implementation below was confirmed directly against
dev.streamlabs.com's own reference docs (project notes, Section 7):
  - GET  /points/user_points        -> read a user's current balance
  - POST /points/subtract           -> atomic/relative decrement (confirmed
                                        via docs: "the points you want to
                                        subtract from the user")
  - POST /points/user_point_edit    -> ABSOLUTE SET, not a relative add
                                        (confirmed: "points that will be set
                                        to the user") - there is no dedicated
                                        single-user "add" endpoint, so
                                        granting points is read -> add ->
                                        set, wrapped in the lock below.

NOTE on a couple of details not yet empirically confirmed (flagged rather
than silently assumed, matching the project's "verify, don't assume"
principle - these are checklist items #10/#11 in the project notes).
Neither can be settled until the approval above comes through, since
every call 401s before reaching the logic in question:
  - Whether `user_point_edit` needs a `channel` field like `subtract` does
    (docs didn't show one). Left out here; add it if a real test call
    returns an error asking for it.
  - Exact placement of the access token (header vs query param) - using an
    Authorization header below, per Streamlabs' OAuth docs saying either a
    header or a parameter works.
"""
import asyncio

import aiohttp

import points_cloudbot
import points_local
from config import config
from logger import get_logger

log = get_logger("Points")

BASE_URL = "https://streamlabs.com/api/v2.0/points"

BACKENDS = ("api", "cloudbot", "local")
DEFAULT_BACKEND = "api"

# Global lock (Section 7's confirmed fix for the read-modify-write race
# condition) - functionally equivalent to a serialized queue, since
# asyncio.Lock queues waiters in arrival order. Held in the dispatcher
# below rather than in either backend, because both of them grant points
# by reading a balance and writing back a total derived from it, and both
# are therefore racy in the same way. Only needed around grant, NOT around
# subtract: Streamlabs applies that one server-side as a relative
# decrement, and the local ledger's subtract never yields mid-update.
_grant_lock = asyncio.Lock()


def backend_name() -> str:
    """
    Which ledger is live. Surfaced on /api/status so the dashboard can say
    so out loud - a local ledger reading 0 for a viewer who genuinely has
    thousands of Streamlabs points is not a bug, but it looks exactly like
    one if nothing on screen mentions which ledger is being read.

    An unrecognized value falls back to the default rather than raising:
    this is read on every points call, and a typo in config.json should
    not take chat down with it. It is logged loudly instead.
    """
    name = config.get("points_backend", DEFAULT_BACKEND)
    if name not in BACKENDS:
        log.error(
            f"points_backend is {name!r}, which is not one of {BACKENDS} - "
            f"falling back to {DEFAULT_BACKEND!r}."
        )
        return DEFAULT_BACKEND
    return name


# ---------- Streamlabs REST API backend ----------

def _headers() -> dict:
    token = config.get("streamlabs_access_token", "")
    if not token:
        log.warning("streamlabs_access_token is empty in config.json - points calls will fail auth")
    return {"Authorization": f"Bearer {token}"}


async def _api_get_user_points(username: str) -> int:
    channel = config.get("streamlabs_channel", "")
    params = {"username": username, "channel": channel}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/user_points", params=params, headers=_headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
            log.info(f"Read balance for {username}: {data}")
            return data.get("points", 0)


async def _api_subtract_points(username: str, amount: int) -> None:
    channel = config.get("streamlabs_channel", "")
    body = {"username": username, "channel": channel, "points": amount}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/subtract", json=body, headers=_headers()) as resp:
            resp.raise_for_status()
            log.info(f"Subtracted {amount} points from {username}")


async def _api_set_points_absolute(username: str, new_total: int) -> None:
    body = {"username": username, "points": new_total}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/user_point_edit", json=body, headers=_headers()) as resp:
            resp.raise_for_status()
            log.info(f"Set {username}'s balance to {new_total}")


async def _api_grant_points(username: str, amount: int) -> int:
    """Read -> add -> set. Called with _grant_lock already held."""
    current = await _api_get_user_points(username)
    new_total = current + amount
    await _api_set_points_absolute(username, new_total)
    log.info(f"Granted {amount} points to {username}: {current} -> {new_total}")
    return new_total


# ---------- Public API - identical whichever backend is live ----------

async def get_user_points(username: str) -> int:
    """Read a specific user's current balance. Raises if it can't be read."""
    backend = backend_name()
    if backend == "local":
        return await points_local.get_user_points(username)
    if backend == "cloudbot":
        return await points_cloudbot.get_user_points(username)
    return await _api_get_user_points(username)


async def subtract_points(username: str, amount: int) -> None:
    """
    Decrement, without the grant lock: neither backend implements this as
    a read-modify-write that could interleave (see _grant_lock's comment).
    """
    backend = backend_name()
    if backend == "local":
        return await points_local.subtract_points(username, amount)
    if backend == "cloudbot":
        return await points_cloudbot.subtract_points(username, amount)
    return await _api_subtract_points(username, amount)


async def grant_points(username: str, amount: int) -> int:
    """
    The shared "add points to this user" function - used by BOTH the
    Streamlabs Tips Socket API listener (Task #6, real donations) and the
    admin dashboard's points testing tool (Task #4, manual testing), per
    Section 7/14's design: testing through the dashboard exercises the
    SAME code path as the real thing, not a separate simulation of it.

    Returns the new balance.
    """
    async with _grant_lock:
        backend = backend_name()
        if backend == "local":
            return await points_local.grant_points(username, amount)
        if backend == "cloudbot":
            return await points_cloudbot.grant_points(username, amount)
        return await _api_grant_points(username, amount)
