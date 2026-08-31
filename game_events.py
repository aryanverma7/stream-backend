"""
Live Valorant game state, from the Overwolf app on the gaming PC.

Why this exists at all: every round-level thing this backend does has so
far been inferred from a keystroke. `agent.py` watches for B and guesses
that the buy menu opened; `burst_timer.py` decides by the clock whether a
press begins a new round; `credit_ocr.py` reads Valorant's own "MIN NEXT
ROUND" text off a screenshot. Each of those is a good guess and each one
has been wrong in a way that took a real stream to find - see fixes #6,
#8, #10, #11, #13 and #14 on the gaming PC, and findings #1 through #9
here. They are all the same problem: this stack cannot see the game.

Overwolf's Game Events Provider can. It reports `round_phase` directly
("shopping" / "combat" / "end" / "game_end"), the match score as
{"won": n, "lost": n}, `match_outcome`, and - the one that matters most -
the local player's `money`, which is the number `credit_ocr` exists to
read off a screenshot.

Deliberately NOT wired to the roulette's budget yet. It runs alongside the
OCR pipeline and both numbers are shown on the dashboard, because
"Overwolf reports a field" and "that field is right, on this machine, in
this game mode, at the moment we read it" are different claims and only
the second one is worth deleting a working pipeline over. What it IS wired
to is the buy-phase signal, which is strictly better than the keystroke
guess it joins, and the agent name, which was a chore somebody had to type.

**Money here is not the same number as credit_ocr's.** Valorant's "MIN
NEXT ROUND" is a projection of what you will have next round; GEP's
`money` is what you hold right now. They only agree during a buy phase.
That difference is the reason the roulette is moving to resolve during the
buy phase rather than before it - with the roulette running while
`round_phase` is "shopping", the exact current number IS the budget and
nothing has to be projected at all.

Transport: the app POSTs a full snapshot of the fields we care about
whenever any of them changes, rather than streaming every GEP event. GEP
fires constantly - the scoreboard alone updates on every damage tick - and
almost none of it is interesting here. A whole snapshot rather than a diff
because it is idempotent: a POST that never arrives costs nothing, since
the next one carries the same complete picture. There is no ordering
problem to solve and no resync protocol to get wrong.

The snapshot doubles as the liveness ping. The app also sends one on a
timer with nothing changed, so "have we heard from the gaming PC" and
"what is the game doing" are one mechanism instead of two - unlike the OCR
agent, which needed a separate heartbeat precisely because its captures
only travel during a burst.

Auth: same shared secret as the OCR routes (X-Agent-Secret against
config's ocr_agent_secret), and like them this route MUST be listed in
auth.py's open_paths or the middleware 401s it before its own check ever
runs. That is the single most common way to break something in this
backend.
"""
import time

from aiohttp import web

import roulette
from config import config
from logger import get_logger

log = get_logger("GameEvents")

# Fields the gaming PC sends and this module tracks. Anything else GEP
# offers is deliberately not carried: every field here is one somebody has
# to keep working across a Valorant patch, and the kill feed is not worth
# that on a stack whose job is picking a gun.
_TRACKED_FIELDS = (
    "round_phase",
    "round_number",
    "score",
    "match_outcome",
    "match_id",
    "map",
    "game_mode",
    "money",
    "agent",
)

# How long a snapshot stays meaningful. Three of the app's 15-second
# timed snapshots, the same ratio ocr_agent.HEARTBEAT_TIMEOUT_SECONDS uses
# against the OCR agent's ping, and for the same reason: two may be
# dropped before the dashboard is allowed to call the gaming PC dead.
SNAPSHOT_TIMEOUT_SECONDS = 45

_state: dict = {}
_last_snapshot_at: "float | None" = None
_game_running: bool = False
_app_version: str = ""

# Listeners, kept as three separate lists because they are three genuinely
# different moments and nothing wants all of them. Same fan-out pattern
# credit_ocr.on_new_buy_phase uses, for the same reason: this module has no
# business importing whatever ends up caring.
_buy_phase_listeners: list = []
_round_result_listeners: list = []
_match_result_listeners: list = []


def on_buy_phase(callback) -> None:
    """Register a coroutine to run when round_phase becomes "shopping"."""
    _buy_phase_listeners.append(callback)


def on_round_result(callback) -> None:
    """Register a coroutine called with True (round won) or False (round lost)."""
    _round_result_listeners.append(callback)


def on_match_result(callback) -> None:
    """Register a coroutine called with "victory", "defeat" or "draw"."""
    _match_result_listeners.append(callback)


async def _notify(listeners: list, *args) -> None:
    for callback in listeners:
        try:
            await callback(*args)
        except Exception:
            # A listener must never turn the gaming PC's POST into a
            # failure. From over there a 500 is indistinguishable from the
            # snapshot not landing, and there is nothing useful to retry.
            log.exception("A game-event listener raised - the snapshot itself still applied")


def _agent_secret_ok(request: web.Request) -> bool:
    """
    The same shared secret the OCR routes use, checked the same way. One
    secret rather than two because it is one machine and one trust
    boundary - a second value would be a second thing to get out of sync
    between two configs on two machines, for no gain.
    """
    expected = config.get("ocr_agent_secret", "")
    provided = request.headers.get("X-Agent-Secret", "")
    return bool(expected) and provided == expected


def _round_delta(old_score: dict, new_score: dict) -> "bool | None":
    """
    Whether a round was just won (True), lost (False), or neither (None).

    GEP has no per-round win event - `score` is simply the running
    {"won": n, "lost": n} - so the result is the difference between two
    snapshots. Which is fine, and is the same shape as the buy-phase
    signal: a fact stated plainly instead of inferred from a keystroke.

    A score that goes DOWN is a new match, not a loss. Both numbers reset
    to zero between games, and reading that as thirteen consecutive losses
    would be a spectacular way to settle a bet. Only an increase of exactly
    the kind a round produces counts.
    """
    if not isinstance(old_score, dict) or not isinstance(new_score, dict):
        return None
    old_won, old_lost = old_score.get("won"), old_score.get("lost")
    new_won, new_lost = new_score.get("won"), new_score.get("lost")
    if None in (old_won, old_lost, new_won, new_lost):
        return None
    if new_won > old_won:
        return True
    if new_lost > old_lost:
        return False
    return None


async def _apply(snapshot: dict) -> list:
    """
    Merges a snapshot into the tracked state and fires whatever the change
    means. Returns the field names that actually changed, which is what the
    app gets back - it is the only way, from the gaming PC, to tell "the
    backend has this" apart from "the backend accepted my POST and ignored
    all of it because the field names are wrong."
    """
    global _state

    changed = []
    previous = dict(_state)
    for field in _TRACKED_FIELDS:
        if field not in snapshot:
            continue  # not sent is not the same as sent-as-null
        value = snapshot[field]
        if _state.get(field) != value:
            _state[field] = value
            changed.append(field)

    if not changed:
        return changed

    # A new buy phase. This is the signal the whole gaming-PC keystroke
    # apparatus exists to approximate, arriving as a plain statement of
    # fact - and it fires on EVERY buy phase, including the ones where the
    # streamer never opens the menu at all, which no amount of watching for
    # B could ever have caught.
    if "round_phase" in changed and _state.get("round_phase") == "shopping":
        log.info(f"Buy phase started (round {_state.get('round_number')})")
        await _notify(_buy_phase_listeners)

    if "score" in changed:
        won = _round_delta(previous.get("score") or {}, _state.get("score") or {})
        if won is not None:
            log.info(f"Round {'won' if won else 'lost'} - score now {_state.get('score')}")
            await _notify(_round_result_listeners, won)

    # Only a real outcome, never the clearing of one. GEP leaves this unset
    # between matches, and a transition back to nothing is the next game
    # starting rather than a result to settle anything against.
    if "match_outcome" in changed and _state.get("match_outcome"):
        log.info(f"Match ended: {_state.get('match_outcome')}")
        await _notify(_match_result_listeners, _state.get("match_outcome"))

    # The agent is a config value the streamer used to have to type with
    # !agent every time they switched. Written only on a real change, so
    # this is one config save per match rather than one per snapshot.
    if "agent" in changed and _state.get("agent"):
        stored = roulette.normalize_agent(_state["agent"])
        if stored and stored != roulette.current_agent():
            roulette.set_agent(stored)

    return changed


async def handle_state(request: web.Request) -> web.Response:
    """POST /api/game/state - a full snapshot of the live game, from the Overwolf app."""
    global _last_snapshot_at, _game_running, _app_version

    if not _agent_secret_ok(request):
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Body must be JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Body must be a JSON object"}, status=400)

    _last_snapshot_at = time.time()
    _app_version = str(body.get("app_version", "") or "")

    # Stamped before the state merge, because a game that has closed still
    # sends one last snapshot and the dashboard should show "app running,
    # game closed" rather than a stale match sitting there looking live.
    was_running = _game_running
    _game_running = bool(body.get("game_running", False))
    if was_running and not _game_running:
        log.info("Valorant closed - clearing the live game state")
        _state.clear()

    snapshot = body.get("state")
    changed = await _apply(snapshot) if isinstance(snapshot, dict) else []
    return web.json_response({"status": "ok", "applied": changed})


def is_connected() -> bool:
    """Whether the Overwolf app has been heard from recently enough to believe."""
    if _last_snapshot_at is None:
        return False
    return (time.time() - _last_snapshot_at) <= SNAPSHOT_TIMEOUT_SECONDS


def round_phase() -> "str | None":
    """"shopping", "combat", "end", "game_end", or None when nothing is known."""
    if not is_connected():
        return None
    return _state.get("round_phase")


def in_buy_phase() -> bool:
    return round_phase() == "shopping"


def local_money() -> "int | None":
    """
    The local player's current credits, or None when the game is not being
    watched. NOT the same number as credit_ocr.get_predicted_credits(),
    which is Valorant's projection for NEXT round - see the module
    docstring. Nothing that makes a decision reads this yet.
    """
    if not is_connected():
        return None
    money = _state.get("money")
    return money if isinstance(money, int) else None


def status() -> dict:
    """A snapshot for /api/status, including how stale the last one is."""
    age = None if _last_snapshot_at is None else round(time.time() - _last_snapshot_at, 1)
    return {
        "connected": is_connected(),
        "last_snapshot_age_seconds": age,
        "snapshot_timeout_seconds": SNAPSHOT_TIMEOUT_SECONDS,
        "app_version": _app_version or None,
        "game_running": _game_running,
        "round_phase": _state.get("round_phase"),
        "round_number": _state.get("round_number"),
        "score": _state.get("score"),
        "match_outcome": _state.get("match_outcome"),
        "map": _state.get("map"),
        "game_mode": _state.get("game_mode"),
        "agent": _state.get("agent"),
        # Reported next to credit_ocr's own number on the dashboard, which
        # is the entire point of this field existing before anything reads
        # it: the two run side by side until one of them has earned the
        # right to replace the other.
        "money": _state.get("money"),
    }


def reset() -> None:
    """
    Drops all tracked state and every registered listener. Exists for the
    tests.

    Clearing the listeners is safe precisely because nothing in production
    calls this - main.py registers once at startup and never resets. In a
    test file they are module-level lists like everything else here, so
    without this each test's listener survives into the next one, and a
    test that deliberately registers a raising listener goes on raising for
    the rest of the run.
    """
    global _last_snapshot_at, _game_running, _app_version
    _state.clear()
    _last_snapshot_at = None
    _game_running = False
    _app_version = ""
    _buy_phase_listeners.clear()
    _round_result_listeners.clear()
    _match_result_listeners.clear()


def register_routes(app: web.Application) -> None:
    app.router.add_post("/api/game/state", handle_state)
