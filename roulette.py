"""
Roulette core (Tasks #9/#10/#11) - viewers spend points to force a weapon
buy for the next round, per the actual confirmed mechanic:

  1. Anyone with enough points can trigger a roulette once per round
     (a 90s cooldown stands in for real round-detection, since OCR-based
     round detection - Task #8 - isn't built yet).
  2. Triggering opens a 15-20s voting window where OTHER viewers spend
     points to add weight to specific weapons.
  3. Each weapon's OWN vote cost escalates with each vote it individually
     receives - not a single global escalating cost, a per-weapon one.
  4. Whichever weapon has the most weight when the timer ends is the
     "winner" - the one the streamer is forced to buy next round.
  5. Affordability filtering limits the options to what is actually
     buyable next round, using credit_ocr's predicted credits. The
     affordable set is snapshotted ONCE when the session starts and used
     for the whole voting window - viewers vote against the list they were
     actually shown, and a late OCR reading can't retroactively invalidate
     a vote someone already paid points for.

     Degrades open, never closed: with no prediction available (OCR down,
     no buy phase seen yet, history just reset) every weapon is votable,
     which is the pre-OCR behaviour. Same if the filter is switched off via
     roulette_affordability_filter_enabled, or if a misconfigured creds
     table would otherwise leave nothing votable at all.

Chat commands, via the existing streamerbot.on_event() listener pattern:
  !roulette   - trigger a new session
  !<weapon>   - vote for a weapon during an active session (e.g. !vandal)
"""
import asyncio
import time

import credit_ocr
from config import config
from logger import get_logger
from points import get_user_points, subtract_points
from widget_hub import widget_hub

log = get_logger("Roulette")

# Valorant's actual weapon roster - stable across a long stretch of the
# game's history. Costs are NOT Valorant's own in-game creds - these are
# separate, admin-configurable point values (per spec Section 14's "weapon
# cost tables"), defaulting to a flat value for any weapon not explicitly
# customized in config.
WEAPONS = [
    "classic", "shorty", "frenzy", "ghost", "sheriff",
    "stinger", "spectre",
    "bucky", "judge",
    "bulldog", "guardian", "phantom", "vandal",
    "marshal", "outlaw", "operator",
    "ares", "odin",
]

# Valorant's own in-game creds prices - a DIFFERENT thing from WEAPONS'
# channel-point costs above, and the only place the two systems meet: this
# table decides what is buyable, those decide what a vote costs a viewer.
#
# Riot retunes individual weapon prices from patch to patch (the Marshal,
# Judge, Ares and Stinger have all moved at least once), so every entry is
# overridable per-weapon through config.json's roulette_weapon_creds_costs
# without touching this file - see creds_cost_for().
WEAPON_CREDS_COSTS = {
    "classic": 0,       # always issued free, so always votable
    "shorty": 300,
    "frenzy": 450,
    "ghost": 500,
    "sheriff": 800,
    "stinger": 1100,
    "spectre": 1600,
    "bucky": 850,
    "judge": 1850,
    "bulldog": 2050,
    "guardian": 2250,
    "phantom": 2900,
    "vandal": 2900,
    "marshal": 950,
    "outlaw": 1800,
    "operator": 4700,
    "ares": 1550,
    "odin": 3200,
}

# A weapon with no price entry at all. Deliberately 0, not a large number:
# an unpriced weapon stays votable rather than silently disappearing from
# the wheel, which matches how the rest of this filter fails open.
DEFAULT_WEAPON_CREDS_COST = 0

DEFAULT_TRIGGER_COST = 500
DEFAULT_VOTE_BASE_COST = 50
DEFAULT_VOTE_COST_INCREMENT = 25
DEFAULT_VOTING_DURATION_SECONDS = 18
DEFAULT_COOLDOWN_SECONDS = 90
DEFAULT_FORCED_BUY_QUEUED_SECONDS = 30  # rough stand-in for "the buy phase has probably ended"


class RouletteState:
    """
    Plain state container, not a class with business logic - keeps the
    actual session-management functions below easy to test independently
    of any particular state-storage mechanism.
    """
    def __init__(self):
        self.is_active = False
        self.weights: dict[str, int] = {}
        # Snapshotted at trigger time, not recomputed per vote - see the
        # module docstring's point 5. None means "no session has set one".
        self.votable_weapons: list[str] | None = None
        self.predicted_credits: int | None = None
        self.last_triggered_at: float = 0.0
        self._end_task: asyncio.Task | None = None
        # Task #11's Forced Buy badge state - separate from is_active/weights
        # above, since this persists AFTER a roulette session itself ends.
        self.forced_buy_weapon: str | None = None
        self.forced_buy_phase: str | None = None  # None | "queued" | "active"
        self._forced_buy_task: asyncio.Task | None = None


_state = RouletteState()

# Guards the check-then-subtract sequence in both trigger_roulette() and
# vote() below - without this, two rapid commands from the same or
# different users could both read a "sufficient" balance before either
# deduction actually completes, letting someone spend more than they have.
# Same race condition points.py's own _grant_lock already exists to solve
# for its own read-modify-write pattern.
_spend_lock = asyncio.Lock()


def _now() -> float:
    return time.time()


def is_on_cooldown() -> bool:
    cooldown = config.get("roulette_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
    return (_now() - _state.last_triggered_at) < cooldown


def vote_cost_for(weapon: str) -> int:
    """The escalating cost for the NEXT vote on this specific weapon - base cost plus one increment per existing vote."""
    base_costs = config.get("roulette_weapon_base_costs", {})
    base = base_costs.get(weapon, DEFAULT_VOTE_BASE_COST)
    increment = config.get("roulette_vote_cost_increment", DEFAULT_VOTE_COST_INCREMENT)
    votes_so_far = _state.weights.get(weapon, 0)
    return base + votes_so_far * increment


def creds_cost_for(weapon: str) -> int:
    """
    What this weapon costs in Valorant's own creds. config.json's
    roulette_weapon_creds_costs overrides individual entries, so a patch
    that retunes one gun is a config edit, not a deploy.
    """
    overrides = config.get("roulette_weapon_creds_costs", {})
    if weapon in overrides:
        return overrides[weapon]
    return WEAPON_CREDS_COSTS.get(weapon, DEFAULT_WEAPON_CREDS_COST)


def affordable_weapons(predicted_credits: "int | None") -> list[str]:
    """
    The weapons buyable with predicted_credits, in WEAPONS' own order.

    Every failure path returns the full roster rather than a short one -
    losing the filter is a much smaller problem than a wheel that silently
    drops most of its options because OCR happened to be down:
      - filter switched off in config
      - no prediction yet (OCR down, or no buy phase read since the last
        reset - get_predicted_credits() returns None for both)
      - a creds table so misconfigured that nothing at all is affordable
    """
    if not config.get("roulette_affordability_filter_enabled", True):
        return list(WEAPONS)
    if predicted_credits is None:
        return list(WEAPONS)

    affordable = [w for w in WEAPONS if creds_cost_for(w) <= predicted_credits]
    if not affordable:
        log.warning(
            f"Predicted credits {predicted_credits} made every weapon unaffordable - that shouldn't be possible "
            f"while the Classic is priced at 0, so the creds table is likely misconfigured. Falling back to the "
            f"full roster rather than opening a roulette nobody can vote in."
        )
        return list(WEAPONS)
    return affordable


async def trigger_roulette(username: str) -> dict:
    """
    Starts a new voting session. Returns a result dict rather than raising,
    so the chat-command layer can decide how to log/respond without a
    try/except at every call site.
    """
    if _state.is_active:
        return {"ok": False, "reason": "A roulette is already in progress"}
    if is_on_cooldown():
        return {"ok": False, "reason": "Roulette is on cooldown"}

    cost = config.get("roulette_trigger_cost", DEFAULT_TRIGGER_COST)
    async with _spend_lock:
        # Explicit balance check, rather than assuming Streamlabs' /subtract
        # endpoint itself rejects a subtraction that would go negative -
        # this isn't confirmed either way in points.py's own docs, so
        # checking ourselves first is the safer, predictable behavior
        # regardless of what Streamlabs does server-side.
        try:
            balance = await get_user_points(username)
        except Exception as e:
            log.warning(f"Could not check {username}'s balance for a roulette trigger: {e}")
            return {"ok": False, "reason": "Couldn't verify your points balance right now"}

        if balance < cost:
            return {"ok": False, "reason": f"Need {cost} points, you have {balance}"}

        try:
            await subtract_points(username, cost)
        except Exception as e:
            log.warning(f"{username} tried to trigger roulette but points deduction failed: {e}")
            return {"ok": False, "reason": "Points deduction failed"}

    # Read the prediction once, here, and hold it for the session. The OCR
    # window is still filling in the background while voting runs, so
    # re-reading it per vote would let the votable set shift underneath
    # viewers who have already spent points against the list they were
    # shown.
    predicted = credit_ocr.get_predicted_credits()
    votable = affordable_weapons(predicted)

    _state.is_active = True
    _state.predicted_credits = predicted
    _state.votable_weapons = votable
    _state.weights = {w: 0 for w in votable}
    _state.last_triggered_at = _now()
    await clear_forced_buy()  # a new session starting means any previous badge is now stale

    duration = config.get("roulette_voting_duration_seconds", DEFAULT_VOTING_DURATION_SECONDS)
    await widget_hub.broadcast(
        {
            "type": "roulette_started",
            "triggered_by": username,
            "weapons": votable,
            "duration_seconds": duration,
            # Both new keys are additive - the overlay renders its pie from
            # weight_updated events and ignores everything else on this
            # message, so no widget change is needed for it to keep working.
            "predicted_credits": predicted,
            "weapon_creds_costs": {w: creds_cost_for(w) for w in votable},
        },
        tag="roulette",
    )
    if predicted is None:
        log.info(f"{username} triggered a roulette - voting open for {duration}s, all {len(votable)} weapons votable "
                 f"(no credit prediction available)")
    else:
        log.info(f"{username} triggered a roulette - voting open for {duration}s, {len(votable)}/{len(WEAPONS)} "
                 f"weapons affordable at {predicted} predicted creds")

    _state._end_task = asyncio.create_task(_end_after_delay(duration))
    return {"ok": True}


async def vote(username: str, weapon: str) -> dict:
    weapon = weapon.lower()
    if not _state.is_active:
        return {"ok": False, "reason": "No roulette is currently active"}
    if weapon not in WEAPONS:
        await widget_hub.broadcast(
            {"type": "invalid_vote", "attempted": weapon, "voted_by": username},
            tag="roulette",
        )
        return {"ok": False, "reason": f"'{weapon}' isn't a recognized weapon"}

    # A real weapon, but not one this session opened voting on. Checked
    # against the snapshot taken at trigger time rather than against a
    # fresh prediction, so this answer is the same for the whole window.
    # Separate from the invalid_vote path above and reported with its own
    # event type: "you can't afford that next round" is a different thing
    # to tell a viewer than "that isn't a weapon", and the overlay's
    # handler chain ignores event types it doesn't know, so adding one
    # can't break the existing widget.
    if _state.votable_weapons is not None and weapon not in _state.votable_weapons:
        cost = creds_cost_for(weapon)
        await widget_hub.broadcast(
            {
                "type": "unaffordable_vote",
                "attempted": weapon,
                "creds_cost": cost,
                "predicted_credits": _state.predicted_credits,
                "voted_by": username,
            },
            tag="roulette",
        )
        return {
            "ok": False,
            "reason": f"{weapon} costs {cost} creds, more than the {_state.predicted_credits} predicted for "
                      f"next round",
        }

    # The cost calculation AND the weight increment both live inside this
    # same lock, not just the balance check/subtract - otherwise two rapid
    # votes on the same weapon could both read the same pre-increment
    # weight and pay the same price, rather than the second voter correctly
    # paying more than the first. The escalating cost only actually
    # escalates if the whole read-cost -> spend -> increment sequence is
    # serialized per weapon, not just the spend itself.
    async with _spend_lock:
        cost = vote_cost_for(weapon)
        try:
            balance = await get_user_points(username)
        except Exception as e:
            log.warning(f"Could not check {username}'s balance for a roulette vote: {e}")
            return {"ok": False, "reason": "Couldn't verify your points balance right now"}

        if balance < cost:
            return {"ok": False, "reason": f"Need {cost} points, you have {balance}"}

        try:
            await subtract_points(username, cost)
        except Exception as e:
            log.warning(f"{username} tried to vote for {weapon} but points deduction failed: {e}")
            return {"ok": False, "reason": "Points deduction failed"}

        _state.weights[weapon] += 1
        new_weight = _state.weights[weapon]

    await widget_hub.broadcast(
        {"type": "weight_updated", "weapon": weapon, "weight": new_weight, "voted_by": username},
        tag="roulette",
    )
    log.info(f"{username} voted for {weapon} (new weight: {new_weight}, cost was {cost})")
    return {"ok": True, "new_weight": new_weight}


async def _end_after_delay(duration: float):
    await asyncio.sleep(duration)
    await end_roulette()


async def end_roulette() -> "str | None":
    """Ends the session immediately (also called by the delayed task above), returns the winning weapon."""
    if not _state.is_active:
        return None

    _state.is_active = False
    if _state.weights and max(_state.weights.values()) > 0:
        winner = max(_state.weights, key=_state.weights.get)
    else:
        winner = None  # nobody voted - no forced result

    await widget_hub.broadcast(
        {"type": "roulette_ended", "winner": winner, "final_weights": dict(_state.weights)},
        tag="roulette",
    )
    log.info(f"Roulette ended - winner: {winner or 'none (no votes)'}")

    if winner:
        await _start_forced_buy(winner)

    return winner


async def _start_forced_buy(weapon: str) -> None:
    """
    Task #11's Forced Buy badge, per the confirmed mechanic: shows
    "queued for next round" immediately after a winner is picked, then
    automatically flips to "currently using" after a configurable delay.

    Honest limitation, not silently glossed over: there's no real
    round-detection (that's the OCR task, not built yet), so this timer is
    a stand-in for "the buy phase has probably ended by now" - the exact
    same kind of approximation already used for Roulette's own
    once-per-round cooldown. If the timing feels off in practice, it's a
    single config value to adjust, not a rebuild.
    """
    _state.forced_buy_weapon = weapon
    _state.forced_buy_phase = "queued"

    await widget_hub.broadcast(
        {"type": "forced_buy_queued", "weapon": weapon},
        tag="roulette",
    )

    delay = config.get("forced_buy_queued_duration_seconds", DEFAULT_FORCED_BUY_QUEUED_SECONDS)
    _state._forced_buy_task = asyncio.create_task(_activate_forced_buy_after_delay(weapon, delay))


async def _activate_forced_buy_after_delay(weapon: str, delay: float) -> None:
    await asyncio.sleep(delay)
    # If a NEW roulette has since started (and possibly already produced
    # its own forced buy), don't let this stale, delayed task overwrite
    # a newer, unrelated result.
    if _state.forced_buy_weapon != weapon:
        return

    _state.forced_buy_phase = "active"
    await widget_hub.broadcast(
        {"type": "forced_buy_active", "weapon": weapon},
        tag="roulette",
    )
    log.info(f"Forced buy now active: {weapon}")


async def clear_forced_buy() -> None:
    """Called when a new roulette starts, clearing any previous forced-buy badge state."""
    if _state.forced_buy_weapon is not None:
        await widget_hub.broadcast({"type": "forced_buy_cleared"}, tag="roulette")
    _state.forced_buy_weapon = None
    _state.forced_buy_phase = None


async def handle_chat_command(event: dict):
    """
    Registered via streamerbot.on_event() - parses ChatMessage events for
    !roulette and !<weapon> commands. Matches forward_chat_to_widgets'
    exact event shape, since both listeners subscribe to the same stream.
    """
    if event.get("event", {}).get("type") != "ChatMessage":
        return

    data = event.get("data", {})
    message_data = data.get("message", {})
    username = message_data.get("username", "")
    text = message_data.get("message", "").strip()

    if not text.startswith("!") or not username:
        return

    command = text[1:].lower().split()[0] if len(text) > 1 else ""

    if command == "roulette":
        await trigger_roulette(username)
    elif command in WEAPONS:
        await vote(username, command)
    elif command and _state.is_active:
        # Only treated as a likely mistaken vote attempt (worth feedback)
        # while a session is actually active - otherwise, an unrelated
        # "!word" command (e.g. an unrelated !discord or !lurk from some
        # other bot setup) would get incorrectly flagged as an "invalid
        # weapon" every time it happened to coincide with a live roulette,
        # which isn't what this is meant to catch.
        await vote(username, command)
