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
from points import UnknownUser, grant_points, try_spend
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

# Valorant's five sidearms. On a round with real money they are not picks,
# they are noise: eighteen options is already more than a chat can read off
# an overlay in eighteen seconds, and four of those options are guns nobody
# would choose to be forced into with 4000 creds in the bank. They are
# dropped from the roster off pistol rounds - see affordable_weapons().
PISTOL_WEAPONS = ["classic", "shorty", "frenzy", "ghost", "sheriff"]

# ...with one exception, which is why this is a list and not a flag. The
# Sheriff is a genuine pick at any economy - it is a one-tap sidearm people
# buy deliberately on full-buy rounds - so it stays on the wheel while the
# other four come off.
ALWAYS_VOTABLE_PISTOLS = ["sheriff"]

# Not all of a round's credits are available for a gun. Shields and
# abilities come out of the same wallet and get bought every round, so a
# roster built from the raw reading offers weapons that cannot actually
# be bought alongside them - 5000 creds is an Odin OR a Vandal plus a
# full kit, not both.
# What a full kit costs, per agent. Only the agents whose totals could be
# sourced are here; anything missing falls back to
# DEFAULT_ABILITY_RESERVE_CREDS and says so in the log, so the gap is
# visible rather than silently averaged over.
#
# ALL OF THESE NEED CHECKING AGAINST THE CURRENT PATCH. Riot retunes
# ability prices, new agents ship without entries, and the buy menu in
# game is the only authority. That is why the whole table is overridable
# from config.json (`roulette_agent_ability_costs`) and editable straight
# from the admin dashboard - correcting a price is a text edit, never a
# deploy.
#
# Two shapes are accepted per agent:
#   "jett": 900                                   - a flat kit total
#   "jett": {"cloudburst": {"cost": 200, "charges": 3},
#            "updraft":    {"cost": 150, "charges": 2}}
# The second is summed as cost x charges. Start from the flat number and
# break an agent out into abilities when you want the detail; both are
# read the same way.
#
# Free signature abilities (the E slot for most agents) are deliberately
# NOT counted - they cost nothing at the buy menu, which is the only
# thing this number is for.
AGENT_KIT_CREDS_COSTS = {
    "astra": 600,
    "cypher": 600,
    "killjoy": 600,
    "viper": 600,
    "brimstone": 650,
    "breach": 700,
    "kayo": 700,
    "omen": 700,
    "phoenix": 700,
    "reyna": 700,
    "skye": 700,
    "sova": 700,
    "yoru": 700,
    "raze": 800,
    "sage": 800,
    "jett": 900,
}

DEFAULT_SHIELD_RESERVE_CREDS = 1000     # heavy shield
DEFAULT_ABILITY_RESERVE_CREDS = 400     # rough, agent-independent - see reserved_creds()
# A pistol round issues 800, and nobody buys heavy shield out of that.
# Detected by the number rather than by round tracking, which does not
# exist here: at or below this, the pistol reserve applies instead.
DEFAULT_PISTOL_ROUND_MAX_CREDS = 800
DEFAULT_PISTOL_RESERVE_CREDS = 400      # light shield, and little else

DEFAULT_TRIGGER_COST = 500
DEFAULT_VOTE_BASE_COST = 50
DEFAULT_VOTE_COST_INCREMENT = 25
DEFAULT_VOTING_DURATION_SECONDS = 18
# What every votable weapon is worth on the wheel before anyone votes.
# Each vote adds one more, so at the default a single vote doubles that
# weapon's slice rather than eliminating the rest of the roster. Raise it
# to flatten the odds (at 5, one vote is a 20% edge rather than 100%);
# lower it towards 0 to make votes decisive, which is the behaviour this
# replaced.
DEFAULT_BASE_WHEEL_SHARE = 1
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
        # When the last buy-phase signal was accepted, for the debounce in
        # on_new_buy_phase(). Two sources report the same phase, so this is
        # what keeps one round from counting as two.
        self.last_buy_phase_at: float = 0.0
        # What the last completed session produced, kept after the session
        # itself is over so the dashboard can answer "which gun am I being
        # forced into" without the streamer having to alt-tab to their own
        # overlay to find out. Not the same thing as forced_buy_weapon
        # above, which is the badge's state and is cleared two buy phases
        # later - this survives until the next roulette replaces it.
        self.last_result: dict | None = None


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


def wheel_shares() -> dict:
    """
    How much of the wheel each votable weapon occupies.

    Every weapon on the session's roster gets `roulette_base_wheel_share`
    to begin with, and each vote adds one more. A vote is therefore a
    thumb on the scale, not an elimination: one vote for the vandal in an
    18-weapon session makes it 2/19 of the wheel while the other
    seventeen hold 1/19 each, which is what a viewer paying 50 points for
    "more chance" is actually buying.

    This replaced taking the single highest weight, which made the first
    vote decide the round outright - the other seventeen weapons were
    still listed, still votable, and could no longer win. It also folds
    the no-votes case in rather than special-casing it: with no votes
    every share is the base, and a uniform wheel is exactly what an
    unvoted roulette is.
    """
    base = config.get("roulette_base_wheel_share", DEFAULT_BASE_WHEEL_SHARE)
    return {weapon: base + votes for weapon, votes in _state.weights.items()}


def draw_winner(shares: dict) -> "str | None":
    """
    Spins the wheel described by `shares`.

    sorted() rather than the dict's own order so a seeded random is
    reproducible in tests - the roster's order depends on the
    affordability snapshot, which depends on OCR.
    """
    weapons = sorted(shares)
    if not weapons:
        return None
    weights = [shares[w] for w in weapons]
    if sum(weights) <= 0:
        return None
    return random.choices(weapons, weights=weights, k=1)[0]


def _odds_text(shares: dict, winner: str) -> str:
    """The winner's share of the wheel, for the chat announcement."""
    total = sum(shares.values())
    if total <= 0:
        return "no odds"
    return f"{round(100 * shares[winner] / total)}% of the wheel"


def _unreachable_wallet(platform: str) -> "str | None":
    """
    The refusal for a viewer whose chat this backend is configured not to
    charge in, or None when they are fine.

    Empty by default: every platform is attempted, because the spend now
    goes to the VIEWER's own chat rather than to one configured one, and
    Cloudbot can only resolve a username in the chat the command was
    typed in. An earlier version of this refused every non-Twitch viewer
    outright, on evidence that turned out to be about our own bug - the
    YouTube spends it was "protecting" against had all been sent to
    Twitch chat, where that handle does not exist.

    `cloudbot_platforms` exists as a way to switch a platform back off
    without a deploy, if one really does prove unusable.
    """
    allowed = config.get("cloudbot_platforms", None)
    if not allowed:
        return None
    if (platform or "").lower() in {str(p).lower() for p in allowed}:
        return None
    usable = ", ".join(str(p) for p in allowed)
    return f"Points aren't set up for {platform} yet - the roulette runs on {usable}"


def _unknown_user_message() -> str:
    """
    What a viewer is told when the points ledger has never heard of them.

    Worth its own message rather than folding into the generic failure,
    because it is the one failure here the viewer can fix themselves:
    Cloudbot only knows people it has seen chat, and it is asked in the
    viewer's own chat, so saying anything at all is the whole fix.
    """
    return (
        "The points bot has no record of you yet - say something in chat "
        "and try again in a moment"
    )


async def _refund(username: str, amount: int, platform: str, why: str) -> None:
    """
    Gives back points taken for something that then didn't happen.

    Points are spent BEFORE the session is set up, because a viewer who
    can't pay must not be able to start one - which leaves a window where
    the money is gone and the roulette isn't running yet. Anything that
    fails in that window has to put it back, or the trigger cost bought
    nothing and the viewer has no way to tell that from bad luck.

    Best effort by design: this is already the error path, so a refund
    that fails too must not replace the original error. It is logged at
    error level with the amount, which is the one case here a human has
    to fix by hand.
    """
    try:
        await grant_points(username, amount, platform=platform)
        log.info(f"Refunded {amount} points to {username} - {why}")
    except Exception:
        log.exception(
            f"REFUND FAILED: {username} paid {amount} points and {why}, and the points "
            f"could not be returned. Give them back by hand."
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


def normalize_agent(name: str) -> str:
    """
    Agent names as this module keys them: lowercase, letters and digits
    only. KAY/O is typed "kayo", "kay/o" and "Kay-O" by different people
    and is one agent in all three cases.
    """
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def agent_kit_cost(agent: str) -> "int | None":
    """
    What `agent`'s buyable abilities cost for one round, or None if this
    agent has no entry anywhere.

    config's `roulette_agent_ability_costs` is checked first and wins
    outright, so a price Riot changed is fixed from the dashboard. Both
    the flat-total and per-ability shapes are accepted - see
    AGENT_KIT_CREDS_COSTS.
    """
    key = normalize_agent(agent)
    if not key:
        return None

    overrides = config.get("roulette_agent_ability_costs", {}) or {}
    entry = None
    for name, value in overrides.items():
        if normalize_agent(name) == key:
            entry = value
            break
    if entry is None:
        entry = AGENT_KIT_CREDS_COSTS.get(key)
    if entry is None:
        return None

    if isinstance(entry, dict):
        total = 0
        for ability in entry.values():
            if isinstance(ability, dict):
                total += int(ability.get("cost", 0)) * int(ability.get("charges", 1))
            else:
                # A bare number per ability - one charge, which is the
                # common case and not worth making people spell out.
                total += int(ability)
        return total
    return int(entry)


def current_agent() -> "str | None":
    """
    The agent the streamer is playing, if anyone has said. Set by the
    !agent chat command or the dashboard, and remembered in config so it
    survives a restart mid-session.
    """
    return config.get("roulette_current_agent", None) or None


def set_agent(name: str) -> "tuple[str, int | None]":
    """
    Records the agent being played and returns (stored name, kit cost).

    Written to config rather than held in memory because it changes once
    per match, not once per round: a backend restart between matches
    should not quietly go back to guessing.

    An agent with no price entry is still accepted. Knowing the name is
    worth something on its own - it shows up on the dashboard and in the
    log line that says the fallback is being used, which is how the
    missing entry gets noticed and filled in.
    """
    stored = normalize_agent(name)
    config.set("roulette_current_agent", stored)
    config.save()
    cost = agent_kit_cost(stored)
    if cost is None:
        log.warning(
            f"Agent set to {stored!r}, which has no entry in roulette_agent_ability_costs - "
            f"falling back to {config.get('roulette_ability_reserve_creds', DEFAULT_ABILITY_RESERVE_CREDS)} "
            f"creds for abilities. Add one from the dashboard to make the roster exact."
        )
    else:
        log.info(f"Agent set to {stored!r} - reserving {cost} creds for abilities")
    return stored, cost


def ability_reserve_creds() -> int:
    """
    What to hold back for abilities: the current agent's real kit cost
    when it is known, and a flat average when it isn't.

    The flat number is a fallback, not a default worth keeping - it is
    wrong for every agent by construction, since kits range from 600 to
    900 and that spread is a whole tier of weapon.
    """
    cost = agent_kit_cost(current_agent() or "")
    if cost is not None:
        return cost
    return config.get("roulette_ability_reserve_creds", DEFAULT_ABILITY_RESERVE_CREDS)


def reserved_creds(predicted_credits: int) -> int:
    """
    Credits that are spoken for before any gun is bought.

    Shields and abilities come out of the same wallet every round, so the
    votable roster has to be built from what is left rather than from the
    whole reading - otherwise a 5000-cred round offers the Odin, and
    buying it means going in with no shield and no kit.

    The pistol-round case is separated by the number, because there is no
    round tracking here to ask: 800 creds is the pistol-round issue and
    nobody buys heavy shield out of it.
    """
    if predicted_credits <= config.get("roulette_pistol_round_max_creds", DEFAULT_PISTOL_ROUND_MAX_CREDS):
        return config.get("roulette_pistol_reserved_creds", DEFAULT_PISTOL_RESERVE_CREDS)
    shield = config.get("roulette_shield_reserve_creds", DEFAULT_SHIELD_RESERVE_CREDS)
    return shield + ability_reserve_creds()


def is_pistol_round(predicted_credits: "int | None") -> bool:
    """
    Whether this looks like a pistol round, decided by the size of the
    budget because there is no round tracking here to ask.

    The same threshold reserved_creds() uses, read from the same config
    key, so the two can never disagree about which kind of round this is -
    a roster built for a pistol round out of a full-buy reserve would be
    wrong in both directions at once.

    Unknown credits are NOT a pistol round: every other unknown here opens
    the full roster, and answering True would do the opposite, quietly
    trimming the wheel on the strength of a reading that does not exist.
    """
    if predicted_credits is None:
        return False
    return predicted_credits <= config.get("roulette_pistol_round_max_creds", DEFAULT_PISTOL_ROUND_MAX_CREDS)


def _pistols_to_hide() -> set:
    """
    The sidearms that come off the wheel on a non-pistol round. Both
    halves are config-overridable: which weapons count as pistols, and
    which of them survive anyway.
    """
    pistols = config.get("roulette_pistol_weapons", PISTOL_WEAPONS) or []
    keep = config.get("roulette_always_votable_pistols", ALWAYS_VOTABLE_PISTOLS) or []
    return {str(w).lower() for w in pistols} - {str(w).lower() for w in keep}


def spendable_creds(predicted_credits: "int | None") -> "int | None":
    """
    What is actually available for a gun, or None when nothing is known.

    Floored at zero rather than allowed to go negative: a negative budget
    would make even the Classic unaffordable and trip
    affordable_weapons()' misconfiguration fallback, which opens the FULL
    roster - the opposite of what a viewer with no money should see.
    """
    if predicted_credits is None:
        return None
    return max(predicted_credits - reserved_creds(predicted_credits), 0)


def affordable_weapons(predicted_credits: "int | None") -> list[str]:
    """
    The weapons buyable with predicted_credits, in WEAPONS' own order.

    Buyable ALONGSIDE a shield and abilities - the roster is built from
    spendable_creds(), not from the raw reading, since all three come out
    of the same wallet in the same buy phase.

    Off a pistol round the sidearms are dropped as well, Sheriff aside -
    see _pistols_to_hide(). That is a taste filter rather than an
    affordability one, and it is the only trim here that can be switched
    off on its own (`roulette_hide_pistols_off_pistol_rounds`).

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

    budget = spendable_creds(predicted_credits)
    affordable = [w for w in WEAPONS if creds_cost_for(w) <= budget]

    # Off a pistol round the sidearms come off the wheel (bar the Sheriff),
    # because with real money in the bank they are not choices anyone wants
    # to be forced into and they crowd out the ones that are.
    #
    # Applied AFTER the price filter and only when something survives it,
    # which is what keeps a save round sane: at 500 spendable the only
    # affordable weapons ARE pistols, and trimming them would leave nothing
    # and fall through to the misconfiguration path below, opening all
    # eighteen. On that round pistols are genuinely the roster.
    if config.get("roulette_hide_pistols_off_pistol_rounds", True) and not is_pistol_round(predicted_credits):
        trimmed = [w for w in affordable if w not in _pistols_to_hide()]
        if trimmed:
            affordable = trimmed

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
            paid, balance = await try_spend(username, cost, platform=platform)
        except UnknownUser:
            log.warning(f"{username} tried to trigger roulette but the points ledger has no record of them")
            return {"ok": False, "reason": _unknown_user_message()}
        except Exception as e:
            log.warning(f"{username} tried to trigger roulette but the points spend failed: {e}")
            return {"ok": False, "reason": "Couldn't take your points right now - try again in a moment"}

        if not paid:
            return {"ok": False, "reason": _too_poor(cost, balance)}

    # Paid. Everything from here to the broadcast is setup, and if any of
    # it fails the viewer is out the trigger cost with nothing running -
    # so it is all inside one try, and the failure path gives the points
    # back and leaves no half-started session behind.
    try:
        return await _open_session(username, platform, cost)
    except Exception:
        log.exception(f"{username} paid {cost} points but the roulette could not be opened")
        _state.is_active = False
        _state.weights = {}
        await _refund(username, cost, platform, "the roulette could not be opened")
        return {"ok": False, "reason": "Something went wrong opening the roulette - your points are back"}


async def _open_session(username: str, platform: str, cost: int) -> dict:
    """
    Sets up a paid-for session. Split out of trigger_roulette purely so
    the paid path has one boundary to catch at - see the call site.
    """
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
            # What is left for a gun after shields and abilities, which is
            # what the roster was actually built from - so the overlay can
            # show the real budget rather than a number no weapon on the
            # wheel was measured against.
            "spendable_credits": spendable_creds(predicted),
            "weapon_creds_costs": {w: creds_cost_for(w) for w in votable},
            # What every weapon is worth on the wheel before any votes,
            # so the overlay draws the same odds the draw will use rather
            # than hardcoding a number that can drift from config.
            "base_wheel_share": config.get("roulette_base_wheel_share", DEFAULT_BASE_WHEEL_SHARE),
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
            paid, balance = await try_spend(username, cost, platform=platform)
        except UnknownUser:
            log.warning(f"{username} tried to vote for {weapon} but the points ledger has no record of them")
            return {"ok": False, "reason": _unknown_user_message()}
        except Exception as e:
            log.warning(f"{username} tried to vote for {weapon} but the points spend failed: {e}")
            return {"ok": False, "reason": "Couldn't take your points right now - try again in a moment"}

        if not paid:
            return {"ok": False, "reason": _too_poor(cost, balance)}

        # The spend is a chat round trip - up to a couple of seconds - and
        # the voting window can close inside it. Without this the viewer
        # pays for a vote that lands on a session already drawn, or on a
        # roster that has since been replaced.
        if not _state.is_active or weapon not in _state.weights:
            await _refund(username, cost, platform, "voting closed before the vote landed")
            return {
                "ok": False,
                "reason": "Voting closed before your vote landed - your points are back",
            }

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
    shares = wheel_shares()

    # The one case that still ends with nothing: a streamer who would
    # rather a silent chat produced no forced buy at all than a weapon
    # nobody asked for. Off by default, because a trigger that buys
    # silence is a trigger nobody pays twice.
    if not anyone_voted and not config.get("roulette_random_pick_when_no_votes", True):
        winner = None
    else:
        winner = draw_winner(shares)
    randomly_picked = winner is not None and not anyone_voted

    await widget_hub.broadcast(
        {
            "type": "roulette_ended",
            "winner": winner,
            "final_weights": dict(_state.weights),
            # The wheel the draw was actually made on. Every votable
            # weapon is present, votes only change how much room each one
            # gets - so the overlay can render the real odds instead of
            # inferring them from vote counts and dropping everything at
            # zero.
            "wheel_shares": shares,
            # Additive, so an older overlay ignores it and still renders
            # the winner. It exists so the overlay can say "no votes" out
            # loud rather than presenting a uniform draw as a vote result -
            # those are different things and a viewer who voted for nothing
            # should not be shown a winner that looks earned.
            "randomly_picked": randomly_picked,
        },
        tag="roulette",
    )
    _state.last_result = {
        "winner": winner,
        "randomly_picked": randomly_picked,
        "final_weights": dict(_state.weights),
        "wheel_shares": shares,
        "predicted_credits": _state.predicted_credits,
        "platform": _state.platform,
        "ended_at": _now(),
    }
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
        await _reply_in_chat(
            _state.platform,
            f"The wheel landed on {winner} ({_odds_text(shares, winner)}). Forced buy next round.",
        )
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
    # Cleared with the counter it guards. A roulette ends mid-round, which
    # can be seconds after a buy-phase signal that has nothing to do with
    # this badge - and if that timestamp survived, the NEXT phase, the one
    # the weapon actually gets bought in, would be debounced away.
    _state.last_buy_phase_at = 0.0

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


# Two buy-phase signals closer together than this are one buy phase being
# reported twice, not two rounds. There are now two independent sources -
# the OCR agent's /api/ocr/reset, driven by a B press, and the Overwolf
# app's round_phase going "shopping" - and on a stream where both are
# running EVERY phase arrives twice. Without this the badge would count
# two phases per round and vanish a round early, which is the one job it
# has.
#
# The number is the same fact burst_timer.NEW_ROUND_GAP_SECONDS states
# from the gaming PC: a Valorant round cannot be won, ended and followed
# by a fresh buy phase inside twenty seconds, so anything arriving inside
# that window is the phase already in progress.
NEW_BUY_PHASE_DEBOUNCE_SECONDS = 20


async def on_new_buy_phase() -> None:
    """
    Registered from main.py against BOTH credit_ocr.on_new_buy_phase() and
    game_events.on_buy_phase() - the two things that can tell this backend
    a round has begun, and the forced-buy badge is what depends on knowing.

    Deliberately registered against both rather than whichever looks
    better. They fail in completely different ways: the OCR agent's signal
    needs the streamer to actually press B, so a round where they never
    open the buy menu produces nothing at all, while the Overwolf app's
    needs Overwolf to be installed, running, and not broken by today's
    Valorant patch. Either one alone advances the badge; the debounce above
    is what stops both of them advancing it twice.

    The badge's life is exactly two buy phases: the first is the one the
    forced weapon actually gets bought in, so the badge becomes "active";
    by the second, the round it belonged to is over and it goes. The
    timers in _start_forced_buy/_activate_forced_buy stay as the fallback
    for a stream where neither source is running - they are approximations
    of this event, so this supersedes whichever one is pending rather
    than racing it.
    """
    if _state.forced_buy_weapon is None:
        return

    now = _now()
    since_last = now - _state.last_buy_phase_at
    if _state.last_buy_phase_at and since_last < NEW_BUY_PHASE_DEBOUNCE_SECONDS:
        log.debug(f"Second buy-phase signal {since_last:.1f}s after the first - same phase, ignoring")
        return
    _state.last_buy_phase_at = now

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


def status() -> dict:
    """
    Everything the admin dashboard needs to answer "what is the roulette
    doing" without opening the stream.

    Three separate things, deliberately not collapsed into one:

      `active` is the session that is open for votes right now, with the
      live weights, so the panel shows the wheel filling up.

      `last_result` is what the previous session landed on. It outlives
      the session AND the badge, because the question it answers - which
      gun am I supposed to be buying - is asked during the buy phase,
      after the overlay has finished its spin and gone.

      `forced_buy` is the badge's own state machine, which is a different
      clock again: queued, then active for the round, then cleared.

    `winner_share` is computed here rather than sent as a fraction so the
    panel and the chat announcement quote the same number - _odds_text()
    rounds the same way.
    """
    result = None
    if _state.last_result is not None:
        result = dict(_state.last_result)
        result["age_seconds"] = round(_now() - result.pop("ended_at"), 1)
        shares = result.get("wheel_shares") or {}
        total = sum(shares.values())
        winner = result.get("winner")
        result["winner_share_percent"] = (
            round(100 * shares[winner] / total) if winner and total > 0 and winner in shares else None
        )
        result["total_votes"] = sum((result.get("final_weights") or {}).values())

    active = None
    if _state.is_active:
        active = {
            "weights": dict(_state.weights),
            "wheel_shares": wheel_shares(),
            "predicted_credits": _state.predicted_credits,
            "platform": _state.platform,
            "seconds_elapsed": round(_now() - _state.last_triggered_at, 1),
        }

    return {
        "active": active,
        "last_result": result,
        "forced_buy": {
            "weapon": _state.forced_buy_weapon,
            "phase": _state.forced_buy_phase,
        },
        "on_cooldown": is_on_cooldown(),
    }


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
    elif command == "agent":
        # Once per match, not once per round - so a typed name costs
        # almost nothing, and is far more reliable than a second OCR
        # target would be. The buy menu does show ability prices, but
        # reading four more numbers multiplies every failure mode the
        # credit reader already has, for a value that changes this rarely.
        await _reply_in_chat(platform, _handle_agent_command(username, text))
    elif command in ("help", "commands", "command"):
        # Answered regardless of whether a session is active - this is
        # the one command a viewer needs to be able to reach at any time,
        # since it's the only way they find out !roulette exists at all.
        await _reply_in_chat(platform, _help_message())
    elif command and _state.is_active and len(text.split()) == 1:
        # Only treated as a likely mistaken vote attempt (worth feedback)
        # while a session is actually active - otherwise, an unrelated
        # "!word" command (e.g. an unrelated !discord or !lurk from some
        # other bot setup) would get incorrectly flagged as an "invalid
        # weapon" every time it happened to coincide with a live roulette,
        # which isn't what this is meant to catch.
        #
        # And only when the whole message is that one word. A vote is
        # always bare - "!vandal", never "!vandal please" - so anything
        # carrying arguments belongs to some other command, and answering
        # "that isn't a recognized weapon" to "!sr blinding lights" is
        # both wrong and the kind of wrong that looks like the song
        # request feature is broken.
        result = await vote(username, command, platform=platform)
        if not result.get("ok"):
            await _reply_in_chat(platform, f"@{username} {result['reason']}")


def _agent_command_is_allowed(username: str) -> bool:
    """
    Who may set the agent.

    Streamer.bot's chat payload carries no role here, so moderator status
    cannot be read - the allowlist is explicit instead. It defaults to the
    channel owner alone, because an open !agent would let anyone reshape
    the votable roster by claiming a 900-cred kit.
    """
    allowed = config.get("roulette_agent_command_users", None)
    if allowed is None:
        owner = config.get("streamlabs_channel", "")
        allowed = [owner] if owner else []
    return username.lower() in {str(name).lower() for name in allowed}


def _handle_agent_command(username: str, text: str) -> str:
    """Answers !agent, and !agent <name>."""
    parts = text.split()
    if len(parts) < 2:
        agent = current_agent()
        if agent is None:
            return f"@{username} No agent set - abilities are being estimated at {ability_reserve_creds()} creds"
        cost = agent_kit_cost(agent)
        if cost is None:
            return f"@{username} Playing {agent} (no ability prices on file - estimating {ability_reserve_creds()} creds)"
        return f"@{username} Playing {agent} - {cost} creds of abilities reserved each round"

    if not _agent_command_is_allowed(username):
        return f"@{username} Only the streamer can set the agent"

    stored, cost = set_agent(parts[1])
    if cost is None:
        return (
            f"@{username} Agent set to {stored} - no ability prices on file for them, "
            f"so abilities stay estimated at {ability_reserve_creds()} creds"
        )
    return f"@{username} Agent set to {stored} - reserving {cost} creds for abilities each round"


def _help_message() -> str:
    """
    Answers !help / !commands. Lists the exact same weapon spelling
    !<weapon> checks against, since a viewer guessing at a name (!ares vs
    !aries) is the other half of the discoverability problem - telling
    them !roulette exists doesn't help if they still can't spell the gun.
    """
    cost = config.get("roulette_trigger_cost", DEFAULT_TRIGGER_COST)
    weapons = ", ".join(WEAPONS)
    # Song requests are a separate module and this is the only place a
    # viewer finds out a command exists, so the line is built here rather
    # than leaving the feature undiscoverable. Imported inside the
    # function: roulette must not depend on spotify at module level, since
    # spotify imports points and points has no business pulling in the
    # roulette's import graph.
    import spotify

    songs = ""
    if spotify.is_configured() and spotify.requests_enabled():
        songs = f" !sr <song> ({spotify.request_cost()} points) queues a track; !song says what's playing."
    # Deliberately does NOT start with "!". Chat replies come back down
    # the subscription as ordinary chat events, and this one used to open
    # with "!roulette", so the bot answered its own !help by parsing it as
    # a !roulette trigger. streamerbot_client drops echoes of our own
    # messages now, which is the real fix; this is the second lock on the
    # same door, and it costs one word.
    return (
        f"Commands: !roulette ({cost} points) opens a vote for next round's forced buy - "
        f"vote with !<weapon> while it's open.{songs} Weapons: {weapons}."
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
