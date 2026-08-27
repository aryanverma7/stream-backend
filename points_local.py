"""
Local points ledger - a stand-in for Streamlabs' Loyalty Points API.

Exists because Streamlabs gates that API behind a manual approval step
that has nothing to do with OAuth scopes. A correctly-issued token
carrying points.read and points.write still comes back:

    401 "Access to Loyalty points API, requires special approval. Please
    request for loyalty access from third party app (OAuth Clients)
    dashboard. We will review and get back to you."

Nothing in this backend can shorten that wait, and the roulette cannot be
tested at all without SOME ledger behind it, so this is one: a flat JSON
file, the same three operations points.py exposes, no network.

The tradeoff is worth stating rather than discovering. This ledger is NOT
the balance viewers see when they type !points in chat. It starts empty
and it does not accrue with watch time - only the admin dashboard's grant
tool and Streamlabs tips put anything into it. That makes it right for
testing the roulette end to end and wrong as a permanent home for viewer
points, which is why points.py keeps the Streamlabs implementation
alongside it behind a config switch rather than replacing it.

Usernames are keyed lowercase. Twitch hands us a login, YouTube hands us
a display name, and the same person typing !roulette twice must not end
up holding two separate balances.
"""
import json
from pathlib import Path

from config import config
from logger import get_logger

log = get_logger("PointsLocal")

DEFAULT_LEDGER_PATH = Path(__file__).parent / "points_local.json"

# Loaded on first use rather than at import, so a test can point
# _ledger_path() somewhere else before anything touches the real file.
_balances: "dict[str, int] | None" = None


def _ledger_path() -> Path:
    return Path(config.get("points_local_file", str(DEFAULT_LEDGER_PATH)))


def _load() -> "dict[str, int]":
    """
    Reads the ledger, treating a missing file as an empty one - that is
    the normal state on a fresh install, not an error.

    A file that exists but doesn't parse IS an error, and it is raised
    rather than swallowed: silently starting from zero would wipe every
    balance on disk the moment anything wrote back.
    """
    global _balances
    if _balances is not None:
        return _balances

    path = _ledger_path()
    if not path.exists():
        _balances = {}
        return _balances

    with open(path, "r") as f:
        raw = json.load(f)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a JSON object of username -> points")
    _balances = {str(k).lower(): int(v) for k, v in raw.items()}
    return _balances


def _save() -> None:
    with open(_ledger_path(), "w") as f:
        json.dump(_load(), f, indent=2, sort_keys=True)


def reset_cache() -> None:
    """Drops the in-memory copy so the next read comes off disk. For tests."""
    global _balances
    _balances = None


async def get_user_points(username: str) -> int:
    """
    A user nobody has ever granted points to has zero, not an error - the
    same answer Streamlabs gives for an unknown viewer, and the answer
    roulette.trigger_roulette() needs in order to say "Need 500 points,
    you have 0" rather than "Couldn't verify your points balance".
    """
    return _load().get(username.lower(), 0)


async def subtract_points(username: str, amount: int) -> None:
    """
    Refuses to take a balance below zero. Streamlabs' own /subtract
    endpoint is undocumented on this point, which is exactly why
    roulette.py checks the balance itself before calling - but a ledger
    that can go negative would let a race past that check hand somebody a
    debt, so the floor is enforced here too.
    """
    balances = _load()
    key = username.lower()
    current = balances.get(key, 0)
    if amount > current:
        raise ValueError(f"{username} has {current} points, cannot subtract {amount}")
    balances[key] = current - amount
    _save()
    log.info(f"Subtracted {amount} points from {username}: {current} -> {balances[key]}")


async def grant_points(username: str, amount: int) -> int:
    """
    Adds to a balance and returns the new total. Called under points.py's
    _grant_lock, same as the Streamlabs implementation, so the
    read-add-write below cannot interleave with another grant.
    """
    balances = _load()
    key = username.lower()
    current = balances.get(key, 0)
    balances[key] = current + amount
    _save()
    log.info(f"Granted {amount} points to {username}: {current} -> {balances[key]}")
    return balances[key]
