"""
Points ledger, with two interchangeable backends behind one API.

`points_backend` in config.json selects between them:

  "cloudbot" (use this) - the wallet viewers actually have: the balance
                       they see when they type !points, accruing on its own
                       with watch time. Reached by asking Streamlabs
                       Cloudbot in chat, because Cloudbot has no API
                       (points_cloudbot.py).
  "api"              - Streamlabs' Loyalty Points REST API. **This is NOT
                       the wallet above**, which was assumed for a long
                       time and is false; see the section below. It is a
                       separate, initially empty ledger that no viewer can
                       see and nothing pays into.
  "local"            - a flat JSON file on this machine (points_local.py).
                       Offline testing only: it holds nothing a viewer
                       earned by watching, so `!points` and the roulette
                       disagree about what anyone has.

**"api" and "cloudbot" are different wallets, confirmed by experiment.**
This was assumed to be one wallet reached two ways for the whole time the
REST API was returning 401, and the assumption survived into comments,
into the tips-listener default, and onto the dashboard. It is wrong.

With Loyalty access finally granted, a read of the channel's whole loyalty
list over REST returned `total: 0` while the Cloudbot dashboard showed
every real viewer. Writing a username that existed nowhere
(`user_point_edit`, 42 points) came back with `"platform": "points"` -
not "twitch", not "youtube" - and that row is visible to the REST API and
absent from the Cloudbot list. Two stores.

Passing `platform` explicitly does not help and is the last thing worth
trying before concluding this: a write sent with `"platform": "twitch"`
comes back with `"platform": "points"` anyway. The field is not an input.
There is no way from these endpoints to address a Cloudbot row.

So the approval this file spent months waiting for does not deliver what
it was waiting for. "api" is `local` with extra steps and a network hop:
persistent and hosted, but holding nothing a viewer earned and nothing
`!points` will ever report. Leave `points_backend` on "cloudbot".

The switch originally existed because Streamlabs gates the Loyalty Points
API behind a manual approval step that is separate from OAuth scopes
entirely. Before that approval, a token issued with points.read and
points.write, for the app owner's own channel, answered every call with:

    401 "Access to Loyalty points API, requires special approval. Please
    request for loyalty access from third party app (OAuth Clients)
    dashboard. We will review and get back to you."

That approval has since been granted, which is how the paragraph above
came to be written: it turned out to unlock the wrong ledger. The switch
stays useful anyway - flipping between backends is a config edit that
takes effect immediately, with no code change here.

---

The endpoints, re-checked against dev.streamlabs.com after the Loyalty
approval landed:
  - GET  /points                    -> read ONE user's balance, by
                                        `username` + `channel`.
  - POST /points/subtract           -> relative decrement, needs `channel`.
                                        Does NOT clamp: a viewer short of
                                        the amount gets a 400 "User does not
                                        have enough points" and nothing is
                                        taken.
  - POST /points/user_point_edit    -> ABSOLUTE SET, not a relative add
                                        ("points that will be set to the
                                        user"), and needs no `channel`.
                                        There is no dedicated single-user
                                        "add", so granting is
                                        read -> add -> set under the lock
                                        below.

Two things this file flagged as unconfirmed while every call was 401ing
both resolved in its favour: `user_point_edit` genuinely takes no
`channel`, and the token genuinely goes in an Authorization header - v2.0
does not accept it as a query parameter at all.

The third guess did NOT survive. The read was written against
`/points/user_points`, which is a different endpoint entirely: it returns
a page of 100 users sorted by points, filtered by a partial name. Reading
a single balance is plain `/points`. Nothing caught it because it 401'd
long before it could 404, and the tests mocked at the function level - so
`TestTheStreamlabsWireFormat` below now pins the URLs themselves.

**Still unverified, and it is the one that broke everything last time:**
how a YouTube viewer is addressed. `GET /points` takes one `channel` and
the response carries a `platform` field, but nothing in the docs says how
- or whether - a viewer who only exists in the YouTube chat is reachable.
Cloudbot kept a separate wallet per platform and could only resolve a name
in the chat the command was typed in; whether the REST API flattens that
or inherits it has to be settled with a real call, not assumed here.
"""
import asyncio

import aiohttp

import points_cloudbot
import points_local
from config import config
from logger import get_logger

log = get_logger("Points")

BASE_URL = "https://streamlabs.com/api/v2.0/points"

# Every call below leaves this machine, and this is the only place in the
# backend where a dashboard request waits on a third party. Without an
# explicit timeout aiohttp allows five minutes, and a handler that sits
# there for five minutes is not a slow answer - the Cloudflare tunnel in
# front of this gives up long before that and serves its own 502 error
# page, which is HTML, which the dashboard then reports as "JSON.parse:
# unexpected character at line 1 column 1". A backend that is up and
# healthy, a panel that says nothing useful, and no log line to connect
# them.
#
# Ten seconds is far longer than a working call to Streamlabs takes and
# far shorter than the tunnel's patience, so a hang becomes a plain error
# on the panel with a matching line in the log.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10


class StreamlabsUnreachable(Exception):
    """
    Raised when the Streamlabs API could not be reached or did not answer
    in time. Its own type, because it means something completely different
    from a request Streamlabs refused: nothing was read and nothing was
    written, so a spend that raises this must never be treated as paid.
    """


def _timeout() -> "aiohttp.ClientTimeout":
    seconds = config.get("streamlabs_api_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS
    return aiohttp.ClientTimeout(total=seconds)


def _session() -> "aiohttp.ClientSession":
    """Every outbound Streamlabs call goes through here, so none can be created without the timeout."""
    return aiohttp.ClientSession(timeout=_timeout())

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
class UnknownUser(Exception):
    """
    The live ledger has no record of this user at all - distinct from
    having no points, and distinct from the ledger being unreachable.

    Backend-neutral on purpose: the cloudbot backend raises its own
    CloudbotUserNotFound, which try_spend() translates, so callers can
    tell a viewer something useful without importing a backend module or
    knowing which one is live.
    """


_grant_lock = asyncio.Lock()

# The same protection for spends. The "api" and "local" backends implement
# try_spend as read-check-write, which is exactly the interleaving hazard
# _grant_lock exists for; the "cloudbot" backend doesn't need it (it never
# reads) but takes it anyway, since one lock that always applies is easier
# to reason about than a rule about which backend is live.
_spend_lock = asyncio.Lock()


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


async def _request(method: str, url: str, **kwargs) -> dict:
    """
    One outbound call to Streamlabs, with the timeout applied and a
    network failure turned into StreamlabsUnreachable.

    Every call goes through here so none can be written without either.
    The translation matters as much as the timeout: aiohttp raises
    asyncio.TimeoutError for a hang, whose str() is the empty string, so
    the dashboard's error path (which renders str(e)) would have shown a
    blank message under a 502 - true, and useless.
    """
    try:
        async with _session() as session:
            async with session.request(method, url, **kwargs) as resp:
                resp.raise_for_status()
                return await resp.json()
    except asyncio.TimeoutError as e:
        raise StreamlabsUnreachable(
            f"Streamlabs did not answer {method} {url} within "
            f"{_timeout().total}s - nothing was read or written."
        ) from e
    except aiohttp.ClientError as e:
        # Includes the response errors raise_for_status() raises, which
        # carry Streamlabs' own status - worth keeping in the message,
        # since a 401 here now means the token expired rather than the
        # Loyalty approval being absent.
        raise StreamlabsUnreachable(f"Streamlabs call failed ({method} {url}): {e}") from e


async def _api_get_user_points(username: str) -> int:
    # BASE_URL itself, not BASE_URL + "/user_points" - that one is the
    # channel leaderboard (a page of 100 users matched on a partial name),
    # and asking it for one balance returns a list that `.get("points")`
    # reads as absent. See the docstring's note on the three guesses.
    channel = config.get("streamlabs_channel", "")
    params = {"username": username, "channel": channel}
    data = await _request("GET", BASE_URL, params=params, headers=_headers())
    log.info(f"Read balance for {username}: {data}")
    return data.get("points", 0)


async def _api_subtract_points(username: str, amount: int) -> None:
    channel = config.get("streamlabs_channel", "")
    body = {"username": username, "channel": channel, "points": amount}
    await _request("POST", f"{BASE_URL}/subtract", json=body, headers=_headers())
    log.info(f"Subtracted {amount} points from {username}")


async def _api_set_points_absolute(username: str, new_total: int) -> None:
    body = {"username": username, "points": new_total}
    await _request("POST", f"{BASE_URL}/user_point_edit", json=body, headers=_headers())
    log.info(f"Set {username}'s balance to {new_total}")


async def _api_try_spend(username: str, amount: int) -> "tuple[bool, int | None]":
    """
    Read -> check -> subtract. Called with _spend_lock already held.

    The read is what lets the refusal say how much the viewer actually
    had, which is the whole difference between "you need 350" and a bare
    no. The subtract is a second line of defence rather than the only one:
    it answers 400 rather than clamping, so a viewer who was topped up or
    drained between the two calls fails loudly instead of being
    part-charged - the exact hole the Cloudbot backend could never close,
    because Cloudbot silently takes whatever is there.
    """
    current = await _api_get_user_points(username)
    if amount > current:
        return False, current
    await _api_subtract_points(username, amount)
    return True, None


async def _api_grant_points(username: str, amount: int) -> int:
    """Read -> add -> set. Called with _grant_lock already held."""
    current = await _api_get_user_points(username)
    new_total = current + amount
    await _api_set_points_absolute(username, new_total)
    log.info(f"Granted {amount} points to {username}: {current} -> {new_total}")
    return new_total


# ---------- Public API - identical whichever backend is live ----------

async def get_user_points(username: str, platform: str = "") -> int:
    """
    Read a specific user's current balance. Raises if it can't be read -
    and under the cloudbot backend that is the normal case for anyone
    whose balance isn't already cached, because Cloudbot's `!points`
    ignores its argument and only ever answers about the account that
    typed it. Nothing that charges points should depend on this; use
    try_spend(), which does not need a read.
    """
    backend = backend_name()
    if backend == "local":
        return await points_local.get_user_points(username)
    if backend == "cloudbot":
        return await points_cloudbot.get_user_points(username, platform)
    return await _api_get_user_points(username)


async def subtract_points(username: str, amount: int, platform: str = "") -> None:
    """
    Decrement, without the grant lock: neither backend implements this as
    a read-modify-write that could interleave (see _grant_lock's comment).
    """
    backend = backend_name()
    if backend == "local":
        return await points_local.subtract_points(username, amount)
    if backend == "cloudbot":
        return await points_cloudbot.subtract_points(username, amount, platform)
    return await _api_subtract_points(username, amount)


async def try_spend(username: str, amount: int, platform: str = "") -> "tuple[bool, int | None]":
    """
    Spend `amount` if the viewer can afford it. The primitive every
    points-charging feature should use.

    Returns (True, None) on success, or (False, balance) when the viewer
    was short, where `balance` is what they actually had - None only if a
    backend genuinely cannot tell. Raises only when the ledger could not
    be reached at all, which callers must treat as "not paid".

    `platform` is the chat the viewer spoke in. It matters only to the
    cloudbot backend, which keeps a separate wallet per platform and can
    only resolve a username in the chat the command is typed in - so the
    command has to go to the viewer's own chat, not to one configured
    one. The other backends ignore it.

    This exists instead of get_user_points-then-subtract_points because
    that pair is a check followed by a separate write, and only some
    backends can support it: the cloudbot backend cannot read a viewer's
    balance at all, so it decides affordability by spending and seeing how
    much Cloudbot took (points_cloudbot.try_spend). Folding both steps
    into one call is what lets each backend answer the question the way
    it actually can.
    """
    async with _spend_lock:
        backend = backend_name()
        if backend == "local":
            return await points_local.try_spend(username, amount)
        if backend == "cloudbot":
            try:
                return await points_cloudbot.try_spend(username, amount, platform)
            except points_cloudbot.CloudbotUserNotFound as e:
                raise UnknownUser(str(e)) from e
        return await _api_try_spend(username, amount)


async def grant_points(username: str, amount: int, platform: str = "") -> "int | None":
    """
    The shared "add points to this user" function - used by BOTH the
    Streamlabs Tips Socket API listener (Task #6, real donations) and the
    admin dashboard's points testing tool (Task #4, manual testing), per
    Section 7/14's design: testing through the dashboard exercises the
    SAME code path as the real thing, not a separate simulation of it.

    Returns the new balance, or None when the live backend confirmed the
    grant but cannot report a total - which the cloudbot backend often
    cannot, since Cloudbot's confirmation reports the amount added and
    there is no way to read a total back. Callers must render that as
    "granted", not as a balance of zero.
    """
    async with _grant_lock:
        backend = backend_name()
        if backend == "local":
            return await points_local.grant_points(username, amount)
        if backend == "cloudbot":
            return await points_cloudbot.grant_points(username, amount, platform)
        return await _api_grant_points(username, amount)
