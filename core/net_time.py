"""
What time the internet thinks it is, read off the Date header of a couple of
big HTTPS sites. Seconds of accuracy, which is all the scheduler's clock gate
needs: it only has to tell "the local clock is right" from "it is days off".
No NTP: that is UDP on a port a network may block, and HTTPS already works.
"""
import asyncio
import statistics
from email.utils import parsedate_to_datetime

import aiohttp

from core.logger import get_logger

log = get_logger("NetTime")

HOSTS = ("https://www.google.com", "https://www.cloudflare.com")
TIMEOUT_SECONDS = 5


def parse_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return None if dt.tzinfo is None else dt.timestamp()  # an HTTP Date is always GMT


async def _ask(session, url: str) -> float | None:
    for method in ("head", "get"):  # GET only if HEAD is refused or carries no Date
        try:
            async with getattr(session, method)(url, allow_redirects=False) as response:
                t = parse_date(response.headers.get("Date"))
        except Exception as e:
            log.debug("%s %s failed: %s", method.upper(), url, e)
            continue
        if t is not None:
            return t
    return None


async def online_time(session_factory=None) -> float | None:
    """Median of the hosts' Date headers (the one answer if only one answered); None if none did."""
    if session_factory is None:
        def session_factory():
            return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))
    try:
        async with session_factory() as session:
            answers = await asyncio.gather(*(_ask(session, u) for u in HOSTS))
    except Exception as e:
        log.debug("Internet time check failed: %s", e)
        return None
    answers = [a for a in answers if a is not None]
    return statistics.median(answers) if answers else None
