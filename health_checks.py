"""
The one status check this backend cannot answer by looking inward.

Everything else on the dashboard's status panel is introspection: the
Streamer.bot socket, the widget connections and the OCR readings all live
inside this process, so reporting them is just reading a variable. Whether
the public hostname actually reaches this process is not like that at all.
That request has to leave the Mac Mini, travel out to Cloudflare, come back
down the tunnel and arrive here - and every one of those hops can be broken
while the backend itself is perfectly healthy.

So this module does exactly that round trip: it fetches our own /health
over the public URL from config.json's `public_base_url` and checks the
answer came from *this* process.

The instance token is what makes that last part worth anything. A tunnel
left running by a previous backend, or a DNS record still pointing at some
other machine, will both answer /health with a perfectly cheerful
{"status": "ok"}; only the token distinguishes "the public URL reaches me"
from "the public URL reaches something". That distinction is the whole
reason this check exists, because the widgets in OBS connect over that same
public hostname - a tunnel pointing at the wrong place shows up on the
panel as zero widget connections and nothing else.

The probe runs on its own timer rather than on each dashboard poll, so
opening the panel never waits on a network round trip, and refreshing it
repeatedly cannot turn into a burst of outbound requests.
"""
import asyncio
import secrets
import time

import aiohttp

from config import config
from logger import get_logger

log = get_logger("HealthChecks")

# Regenerated every start. Not a secret and not used for authentication -
# /health is public - it only has to be different from whatever the last
# process used, so a stale tunnel cannot pass itself off as this one.
INSTANCE_ID = secrets.token_hex(8)

PROBE_INTERVAL_SECONDS = 30
PROBE_TIMEOUT_SECONDS = 8

_reachable: "bool | None" = None
_detail = "Not checked yet."
_checked_at: "float | None" = None
_task: "asyncio.Task | None" = None


def public_health_url() -> "str | None":
    base = config.get("public_base_url", "").strip()
    if not base:
        return None
    return base.rstrip("/") + "/health"


def _record(reachable: "bool | None", detail: str) -> None:
    global _reachable, _detail, _checked_at
    # Only log on a change of state. This runs every 30 seconds forever;
    # logging each result would bury everything else in the file, and the
    # transitions are the only part anyone reads back afterwards.
    if reachable != _reachable:
        if reachable is False:
            log.warning(f"Public URL check failed: {detail}")
        else:
            log.info(f"Public URL check: {detail}")
    _reachable = reachable
    _detail = detail
    _checked_at = time.time()


async def probe_once(session_factory=None) -> None:
    """
    One round trip out to the public hostname and back. Never raises.

    The session factory is injected rather than constructed inline for the
    same reason the timing code elsewhere in this project takes its clock
    as an argument: it makes every branch below - timeout, refusal, wrong
    status, wrong instance - reachable in a test without a network.
    """
    url = public_health_url()
    if url is None:
        _record(None, "public_base_url isn't set in config.json, so there's nothing to check.")
        return

    timeout = aiohttp.ClientTimeout(total=PROBE_TIMEOUT_SECONDS)
    if session_factory is None:
        def session_factory():
            return aiohttp.ClientSession(timeout=timeout)

    try:
        async with session_factory() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    _record(False, f"{url} answered {response.status} instead of 200.")
                    return
                body = await response.json()
    except asyncio.TimeoutError:
        _record(False, f"No answer from {url} within {PROBE_TIMEOUT_SECONDS}s - the tunnel is most likely down.")
        return
    except Exception as e:
        _record(False, f"Couldn't reach {url}: {e}")
        return

    if body.get("instance") != INSTANCE_ID:
        # Reachable, but not us. Left as its own message because the fix is
        # completely different from the tunnel simply being down: something
        # else is answering on this hostname.
        _record(False, f"{url} answered, but from a different backend - check for a stale cloudflared "
                       f"process or a DNS record pointing somewhere else.")
        return

    _record(True, "The public URL reaches this backend.")


async def _probe_loop() -> None:
    while True:
        await probe_once()
        await asyncio.sleep(PROBE_INTERVAL_SECONDS)


async def start() -> None:
    global _task
    if _task is not None:
        return
    # Probed once up front so the panel has a real answer immediately
    # rather than "Not checked yet" for the first half minute after a
    # restart - which is exactly when someone is most likely looking.
    await probe_once()
    _task = asyncio.create_task(_probe_loop())


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None


def status() -> dict:
    return {
        "reachable": _reachable,
        "url": public_health_url(),
        "detail": _detail,
        "checked_age_seconds": None if _checked_at is None else round(time.time() - _checked_at, 1),
    }


def reset() -> None:
    """Clears the cached result. Exists for the tests."""
    global _reachable, _detail, _checked_at
    _reachable = None
    _detail = "Not checked yet."
    _checked_at = None
