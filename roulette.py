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
  !roulette          - trigger a new session
  !<weapon>          - vote for a weapon during an active session (e.g. !vandal)
  !help / !commands  - lists the above, since neither Twitch nor YouTube chat
                        offers autocomplete for a custom command regardless of
                        what parses it - a viewer has no way to discover
                        !roulette or the weapon names except being told
"""
import asyncio
import random
import time

import credit_ocr
from config import config
from logger import get_logger
from points import UnknownUser, backend_name as points_backend_name, try_spend
from streamerbot_client import parse_chat_message, streamerbot
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
# ...and this one for "the round is over, so the gun isn't in play any more".
# Same class of approximation as the two above it, for the same reason: there
# is no real round detection yet. A Valorant round runs 100s after the buy
# phase, so the badge lives roughly as long as the round it describes.
DEFAULT_FORCED_BUY_ACTIVE_SECONDS = 100


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
        # Which chat the session was triggered from, so the winner is
        # announced back there rather than always to Twitch.
        self.platform: str = "twitch"
        self.last_triggered_at: float = 0.0
        self._end_task: asyncio.Task | None = None
        # Task #11's Forced Buy badge state - separate from is_active/weights
        # above, since this persists AFTER a roulette session itself ends.
        self.forced_buy_weapon: str | None = None
        self.forced_buy_phase: str | None = None  # None | "queued" | "active"
        self._forced_buy_task: asyncio.Task | None = None
        # How many new buy phases have been observed since this forced buy
        # was queued. The badge's whole life is two of them - the phase the
        # weapon gets bought in, and the one after, by which point the
        # round it belonged to is over. Counted rather than inferred from
        # forced_buy_phase, because the fallback timers can advance that
        # on their own and then a signal would read the wrong meaning off
        # it: an "active" badge could be one whose buy phase has arrived,
        # or one the timer promoted while nothing was happening.
        self.forced_buy_phases_seen: int = 0


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


def _unreachable_wallet(platform: str) -> "str | None":
    """
    The refusal for a viewer whose chat has no wallet this backend can
    reach, or None when they are fine.

    Checked BEFORE any spend, because the spend cannot succeed and the
    viewer would otherwise wait out the full Cloudbot reply timeout to be
    told something generic - six seconds inside an eighteen-second voting
    window, for someone who was never chargeable.

    Only the cloudbot backend has this problem, and it is not fixable from
    here. Cloudbot resolves a username only within the platform the
    command is typed on, and only among that platform's own users:
    `!addpoints pinkuthagoat` works in Twitch chat, answers "Unable to
    find" in YouTube chat, and a YouTube row is equally unreachable from
    Twitch chat. Streamlabs' dashboard shows both platforms in one Loyalty
    list, which is display-only. See points_cloudbot.py's module docstring
    for the tests behind each of those.
    """
    if points_backend_name() != "cloudbot":
        return None
    ledger = config.get("cloudbot_platform", "twitch")
    if not platform or platform.lower() == ledger.lower():
        return None
    return (
        f"Points are only kept in {ledger} chat right now, so the roulette can't "
        f"charge {platform} viewers - come say hello in {ledger} chat"
    )


def _unknown_user_message() -> str:
    """
    What a viewer is told when the points ledger has never heard of them.

    Worth its own message rather than folding into the generic failure,
    because it is the one failure here the viewer can fix themselves, and
    because it is the expected answer for anyone who only ever watches on
    the other platform: Cloudbot keeps a separate wallet per platform and
    can only be asked about users on `cloudbot_platform`.
    """
    platform = config.get("cloudbot_platform", "twitch")
    return (
        f"The points bot has no record of you - points are kept in {platform} chat, "
        f"so say something there first and try again"
    )


def _too_poor(cost: int, balance: "int | None") -> str:
    """
    The refusal a viewer sees when they can't afford something.

    `balance` is None only when the live points backend cannot tell us
    what they had - saying "you have 0" then would be a guess, and a
    discouraging one, so the number is simply left out.
    """
    if balance is None:
        return f"Need {cost} points"
    return f"Need {cost} points, you have {balance}"


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


async def trigger_roulette(username: str, platform: str = "twitch") -> dict:
    """
    Starts a new voting session. Returns a result dict rather than raising,
    so the chat-command layer can decide how to log/respond without a
    try/except at every call site.

    `platform` is remembered only so the end-of-session announcement lands
    in the chat the trigger came from; it defaults to Twitch, which is
    where every existing caller speaks.
    """
    if _state.is_active:
        return {"ok": False, "reason": "A roulette is already in progress"}
    if is_on_cooldown():
        return {"ok": False, "reason": "Roulette is on cooldown"}

    unreachable = _unreachable_wallet(platform)
    if unreachable is not None:
        return {"ok": False, "reason": unreachable}

    cost = config.get("roulette_trigger_cost", DEFAULT_TRIGGER_COST)
    async with _spend_lock:
        # One call, not a balance check followed by a deduction. Whether
        # the viewer can afford it is the backend's question to answer -
        # the cloudbot backend cannot read a balance at all and decides by
        # spending, so a check here would have nothing to check.
        try:
            paid, balance = await try_spend(username, cost)
        except UnknownUser:
            log.warning(f"{username} tried to trigger roulette but the points ledger has no record of them")
            return {"ok": False, "reason": _unknown_user_message()}
        except Exception as e:
            log.warning(f"{username} tried to trigger roulette but the points spend failed: {e}")
            return {"ok": False, "reason": "Couldn't take your points right now - try again in a moment"}

        if not paid:
            return {"ok": False, "reason": _too_poor(cost, balance)}

    # Read the prediction once, here, and hold it for the session. The OCR
    # window is still filling in the background while voting runs, so
    # re-reading it per vote would let the votable set shift underneath
    # viewers who have already spent points against the list they were
    # shown.
    predicted = credit_ocr.get_predicted_credits()
    votable = affordable_weapons(predicted)

    _state.is_active = True
    _state.platform = platform or "twitch"
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


async def vote(username: str, weapon: str, platform: str = "twitch") -> dict:
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
    unreachable = _unreachable_wallet(platform)
    if unreachable is not None:
        return {"ok": False, "reason": unreachable}

    async with _spend_lock:
        cost = vote_cost_for(weapon)
        try:
            paid, balance = await try_spend(username, cost)
        except UnknownUser:
            log.warning(f"{username} tried to vote for {weapon} but the points ledger has no record of them")
            return {"ok": False, "reason": _unknown_user_message()}
        except Exception as e:
            log.warning(f"{username} tried to vote for {weapon} but the points spend failed: {e}")
            return {"ok": False, "reason": "Couldn't take your points right now - try again in a moment"}

        if not paid:
            return {"ok": False, "reason": _too_poor(cost, balance)}

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
    anyone_voted = bool(_state.weights) and max(_state.weights.values()) > 0
    randomly_picked = False

    if anyone_voted:
        winner = max(_state.weights, key=_state.weights.get)
    elif _state.weights and config.get("roulette_random_pick_when_no_votes", True):
        # Nobody voted, so every weapon on the roster is carrying the same
        # weight - and a field of equal weights still has an outcome. This
        # used to return None and end the session with nothing, which made
        # the trigger cost buy silence: whoever paid it got no forced buy,
        # no result on the overlay, and no reason to ever pay it again on a
        # quiet chat. A uniform draw is what an unvoted roulette actually
        # is, so it is drawn.
        #
        # sorted() rather than the dict's own order so a seeded random is
        # reproducible - the roster's order depends on the affordability
        # snapshot, which depends on OCR.
        winner = random.choice(sorted(_state.weights))
        randomly_picked = True
    else:
        winner = None

    await widget_hub.broadcast(
        {
            "type": "roulette_ended",
            "winner": winner,
            "final_weights": dict(_state.weights),
            # Additive, so an older overlay ignores it and still renders
            # the winner. It exists so the overlay can say "no votes" out
            # loud rather than presenting a uniform draw as a vote result -
            # those are different things and a viewer who voted for nothing
            # should not be shown a winner that looks earned.
            "randomly_picked": randomly_picked,
        },
        tag="roulette",
    )
    log.info(
        f"Roulette ended - winner: {winner or 'none'}"
        f"{' (no votes - picked at random)' if randomly_picked else ''}"
    )

    if winner and randomly_picked:
        await _reply_in_chat(
            _state.platform,
            f"No votes - the wheel landed on {winner}. Forced buy next round.",
        )
        await _start_forced_buy(winner)
    elif winner:
        await _reply_in_chat(_state.platform, f"Roulette locked in: {winner}. Forced buy next round.")
        await _start_forced_buy(winner)
    else:
        await _reply_in_chat(_state.platform, "Roulette closed with no votes - no forced buy this round.")

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
    _state.forced_buy_phases_seen = 0

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
    await _activate_forced_buy(weapon)


async def _activate_forced_buy(weapon: str) -> None:
    _state.forced_buy_phase = "active"
    await widget_hub.broadcast(
        {"type": "forced_buy_active", "weapon": weapon},
        tag="roulette",
    )
    log.info(f"Forced buy now active: {weapon}")

    # And then it has to end. "active" used to be terminal, with
    # clear_forced_buy() called from exactly one place - the start of the
    # NEXT roulette - so the badge sat on stream indefinitely announcing a
    # gun that had long since stopped being in play. It was true for one
    # round and a lie for every round after, and on a night with a single
    # roulette in it, for the rest of the stream.
    linger = config.get("forced_buy_active_duration_seconds", DEFAULT_FORCED_BUY_ACTIVE_SECONDS)
    _state._forced_buy_task = asyncio.create_task(_clear_forced_buy_after_delay(weapon, linger))


async def _clear_forced_buy_after_delay(weapon: str, delay: float) -> None:
    await asyncio.sleep(delay)
    # Same staleness guard as the activation task above: a newer roulette
    # may already have produced its own forced buy, and this task must not
    # clear a badge it has nothing to do with.
    if _state.forced_buy_weapon != weapon:
        return
    await clear_forced_buy()


def _cancel_forced_buy_task() -> None:
    """
    Drops whichever fallback timer is pending. Called when the real
    signal arrives, since those timers exist only to stand in for it.
    """
    task = _state._forced_buy_task
    if task is not None and not task.done():
        task.cancel()
    _state._forced_buy_task = None


async def on_new_buy_phase() -> None:
    """
    Registered against credit_ocr.on_new_buy_phase() from main.py - the
    agent's /api/ocr/reset is the only real "a new round has begun"
    signal this backend gets, and the forced-buy badge is the other thing
    that depends on knowing.

    The badge's life is exactly two buy phases: the first is the one the
    forced weapon actually gets bought in, so the badge becomes "active";
    by the second, the round it belonged to is over and it goes. The
    timers in _start_forced_buy/_activate_forced_buy stay as the fallback
    for a stream where the agent is not running - they are approximations
    of this event, so this supersedes whichever one is pending rather
    than racing it.
    """
    if _state.forced_buy_weapon is None:
        return

    _state.forced_buy_phases_seen += 1
    _cancel_forced_buy_task()

    if _state.forced_buy_phases_seen == 1:
        await _activate_forced_buy(_state.forced_buy_weapon)
    else:
        await clear_forced_buy()


async def clear_forced_buy() -> None:
    """
    Drops the forced-buy badge. Called when the round it describes is
    over - on the real buy-phase signal where one arrives, on the fallback
    timer where it doesn't - and again when a new roulette starts, in case
    that happens first.
    """
    if _state.forced_buy_weapon is not None:
        await widget_hub.broadcast({"type": "forced_buy_cleared"}, tag="roulette")
        log.info(f"Forced buy cleared: {_state.forced_buy_weapon}")
    _state.forced_buy_weapon = None
    _state.forced_buy_phase = None
    _state.forced_buy_phases_seen = 0


async def _reply_in_chat(platform: str, text: str) -> None:
    """
    Answers a viewer in the chat they spoke in. Every refusal path in
    trigger_roulette()/vote() already produces a human-readable `reason`;
    before this existed those strings were computed and then dropped on
    the floor, so somebody who typed !roulette without enough points saw
    absolutely nothing happen and had no way to tell that from the bot
    being down.

    Off by a single config flag, because SendMessage is the one request
    Streamer.bot documents as requiring authentication on its WebSocket
    server - if that is switched on at the gaming PC end, every reply is
    rejected and turning them off is better than logging a rejection per
    command.
    """
    if not config.get("roulette_chat_replies_enabled", True):
        # The last silent path. With this off, every reply was computed
        # and dropped with no trace, which looks exactly like a bot that
        # never tried to answer.
        log.info(f"Chat replies are disabled - not sending: {text!r}")
        return
    await streamerbot.send_chat_message(text, platform=platform or "twitch")


async def handle_chat_command(event: dict):
    """
    Registered via streamerbot.on_event() - parses chat events for
    !roulette and !<weapon> commands, and answers the viewer in chat.
    Shares parse_chat_message() with forward_chat_to_widgets so the two
    listeners cannot disagree about the payload shape.
    """
    chat = parse_chat_message(event)
    if chat is None:
        return

    username = chat["username"]
    text = chat["text"].strip()
    platform = chat["platform"]

    if not text.startswith("!") or not username:
        return

    command = text[1:].lower().split()[0] if len(text) > 1 else ""
    # Logged because nothing else on the receive side was. A command that
    # produced no visible effect gave no way to tell "chat never reached
    # this backend" apart from "it arrived and the handler did nothing" -
    # and those need completely different fixes. Only "!"-prefixed lines
    # get here, so this is not the whole chat.
    log.info(f"Chat command from {username} on {platform}: {text!r} -> {command!r}")

    if command == "roulette":
        result = await trigger_roulette(username, platform=platform)
        if result.get("ok"):
            await _reply_in_chat(platform, _roulette_open_announcement())
        else:
            await _reply_in_chat(platform, f"@{username} {result['reason']}")
    elif command in WEAPONS:
        result = await vote(username, command, platform=platform)
        # A successful vote stays silent on purpose - the overlay already
        # shows it, and one chat line per vote would drown the channel
        # during a busy window.
        if not result.get("ok"):
            await _reply_in_chat(platform, f"@{username} {result['reason']}")
    elif command in ("help", "commands"):
        # Answered regardless of whether a session is active - this is
        # the one command a viewer needs to be able to reach at any time,
        # since it's the only way they find out !roulette exists at all.
        await _reply_in_chat(platform, _help_message())
    elif command and _state.is_active:
        # Only treated as a likely mistaken vote attempt (worth feedback)
        # while a session is actually active - otherwise, an unrelated
        # "!word" command (e.g. an unrelated !discord or !lurk from some
        # other bot setup) would get incorrectly flagged as an "invalid
        # weapon" every time it happened to coincide with a live roulette,
        # which isn't what this is meant to catch.
        result = await vote(username, command, platform=platform)
        if not result.get("ok"):
            await _reply_in_chat(platform, f"@{username} {result['reason']}")


def _help_message() -> str:
    """
    Answers !help / !commands. Lists the exact same weapon spelling
    !<weapon> checks against, since a viewer guessing at a name (!ares vs
    !aries) is the other half of the discoverability problem - telling
    them !roulette exists doesn't help if they still can't spell the gun.
    """
    cost = config.get("roulette_trigger_cost", DEFAULT_TRIGGER_COST)
    weapons = ", ".join(WEAPONS)
    # Deliberately does NOT start with "!". Chat replies come back down
    # the subscription as ordinary chat events, and this one used to open
    # with "!roulette", so the bot answered its own !help by parsing it as
    # a !roulette trigger. streamerbot_client drops echoes of our own
    # messages now, which is the real fix; this is the second lock on the
    # same door, and it costs one word.
    return (
        f"Commands: !roulette ({cost} points) opens a vote for next round's forced buy - "
        f"vote with !<weapon> while it's open. Weapons: {weapons}."
    )


def _roulette_open_announcement() -> str:
    """
    The line posted when a session opens. Reports the budget the same way
    the overlay does, so chat and stream agree: a number when OCR has one,
    and an explicit "every weapon" when it doesn't, rather than quietly
    implying a filter is running when it isn't.
    """
    duration = config.get("roulette_voting_duration_seconds", DEFAULT_VOTING_DURATION_SECONDS)
    weapons = _state.votable_weapons or WEAPONS
    if _state.predicted_credits is None:
        budget = "no credit reading, so every weapon is in play"
    else:
        budget = f"{_state.predicted_credits} creds next round"
    return (
        f"Roulette is open for {duration}s - vote with !weapon. "
        f"{len(weapons)} weapons available ({budget})."
    )
