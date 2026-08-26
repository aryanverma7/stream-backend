"""
Streamlabs Loyalty Points REST API wrapper.

Confirmed directly against dev.streamlabs.com's own reference docs (project
notes, Section 7):
  - GET  /points/user_points        -> read a user's current balance
  - POST /points/subtract           -> atomic/relative decrement (confirmed
                                        via docs: "the points you want to
                                        subtract from the user")
  - POST /points/user_point_edit    -> ABSOLUTE SET, not a relative add
                                        (confirmed: "points that will be set
                                        to the user") - there is no dedicated
                                        single-user "add" endpoint, so
                                        granting points is read -> add ->
                                        set, wrapped in a lock below.

NOTE on a couple of details not yet empirically confirmed (flagged rather
than silently assumed, matching the project's "verify, don't assume"
principle - these are checklist items #10/#11 in the project notes):
  - Whether `user_point_edit` needs a `channel` field like `subtract` does
    (docs didn't show one). Left out here; add it if a real test call
    returns an error asking for it.
  - Exact placement of the access token (header vs query param) - using an
    Authorization header below, per Streamlabs' OAuth docs saying either a
    header or a parameter works. Confirm during Task #5's actual test pass.
"""
import asyncio

import aiohttp

from config import config
from logger import get_logger

log = get_logger("Points")

BASE_URL = "https://streamlabs.com/api/v2.0/points"

# Global lock (Section 7's confirmed fix for the read-modify-write race
# condition) - functionally equivalent to a serialized queue, since
# asyncio.Lock queues waiters in arrival order. Only needed around the
# grant() function below, NOT around subtract() which is already atomic.
_grant_lock = asyncio.Lock()


def _headers() -> dict:
    token = config.get("streamlabs_access_token", "")
    if not token:
        log.warning("streamlabs_access_token is empty in config.json - points calls will fail auth")
    return {"Authorization": f"Bearer {token}"}


async def get_user_points(username: str) -> int:
    """Read a specific user's current balance. Raises on any HTTP error."""
    channel = config.get("streamlabs_channel", "")
    params = {"username": username, "channel": channel}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{BASE_URL}/user_points", params=params, headers=_headers()) as resp:
            resp.raise_for_status()
            data = await resp.json()
            log.info(f"Read balance for {username}: {data}")
            return data.get("points", 0)


async def subtract_points(username: str, amount: int) -> None:
    """
    Atomic decrement - confirmed safe to call directly without the lock,
    since Streamlabs applies this as a relative subtraction server-side,
    not a read-modify-write on our end.
    """
    channel = config.get("streamlabs_channel", "")
    body = {"username": username, "channel": channel, "points": amount}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/subtract", json=body, headers=_headers()) as resp:
            resp.raise_for_status()
            log.info(f"Subtracted {amount} points from {username}")


async def _set_points_absolute(username: str, new_total: int) -> None:
    body = {"username": username, "points": new_total}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{BASE_URL}/user_point_edit", json=body, headers=_headers()) as resp:
            resp.raise_for_status()
            log.info(f"Set {username}'s balance to {new_total}")


async def grant_points(username: str, amount: int) -> int:
    """
    The shared "add points to this user" function - used by BOTH the
    Streamlabs Tips Socket API listener (Task #6, real donations) and the
    admin dashboard's points testing tool (Task #4, manual testing), per
    Section 7/14's design: testing through the dashboard exercises the
    SAME code path as the real thing, not a separate simulation of it.

    Wrapped in the global lock since this is read -> compute -> write,
    which is NOT atomic on Streamlabs' side (see module docstring).
    Returns the new balance.
    """
    async with _grant_lock:
        current = await get_user_points(username)
        new_total = current + amount
        await _set_points_absolute(username, new_total)
        log.info(f"Granted {amount} points to {username}: {current} -> {new_total}")
        return new_total
