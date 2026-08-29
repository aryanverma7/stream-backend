import sys
import random
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock

import roulette
from config import config


@pytest.fixture(autouse=True)
def reset_roulette_state():
    """Every test gets a completely fresh module-level state - this module
    uses shared mutable state by design (matching how a real, single
    ongoing roulette session works), which means tests MUST reset it
    between runs or one test's leftover state silently breaks another."""
    roulette._state = roulette.RouletteState()
    yield
    roulette._state = roulette.RouletteState()


class TestTriggerRoulette:
    @pytest.mark.asyncio
    async def test_succeeds_with_enough_points_and_starts_a_session(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 500})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is True
        assert roulette._state.is_active is True
        assert set(roulette._state.weights.keys()) == set(roulette.WEAPONS)
        assert all(w == 0 for w in roulette._state.weights.values())

    @pytest.mark.asyncio
    async def test_rejects_with_insufficient_points_without_deducting_anything(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 500})
        mock_spend = AsyncMock(return_value=(False, 100))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)

        result = await roulette.trigger_roulette("brokeviewer")

        assert result["ok"] is False
        assert "100" in result["reason"]
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_rejects_a_second_trigger_while_one_is_already_active(self, monkeypatch):
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette.trigger_roulette("first")
        result = await roulette.trigger_roulette("second")

        assert result["ok"] is False
        assert "already in progress" in result["reason"]

    @pytest.mark.asyncio
    async def test_rejects_a_trigger_during_cooldown(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_cooldown_seconds": 90})
        roulette._state.last_triggered_at = roulette._now()  # just triggered

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is False
        assert "cooldown" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_broadcasts_roulette_started_with_the_full_weapon_list(self, monkeypatch):
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        # Pinned explicitly rather than relying on the OCR module's
        # deque happening to be empty - that is another module's shared
        # state, and this test is about the broadcast, not about it.
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: None)

        await roulette.trigger_roulette("someviewer")

        mock_broadcast.assert_called_once()
        payload, kwargs = mock_broadcast.call_args
        assert payload[0]["type"] == "roulette_started"
        assert set(payload[0]["weapons"]) == set(roulette.WEAPONS)
        assert kwargs["tag"] == "roulette"


# Shields and abilities are reserved out of every reading (see
# roulette.reserved_creds), which is its own behaviour with its own tests
# below. The price-filter tests zero it so they are about the prices.
NO_RESERVE = {
    "roulette_shield_reserve_creds": 0,
    "roulette_ability_reserve_creds": 0,
    "roulette_pistol_reserved_creds": 0,
}


class TestAffordableWeapons:
    """
    The pure filter, tested without a session - the trigger/vote paths get
    their own coverage below.
    """

    def test_no_prediction_available_opens_the_whole_roster(self, monkeypatch):
        """
        The single most important behaviour here. get_predicted_credits()
        returns None whenever OCR is down, or the agent has just reset the
        history, or no buy phase has been captured yet - none of which
        should stop viewers using a feature they pay points for.
        """
        monkeypatch.setattr(config, "_data", {})
        assert roulette.affordable_weapons(None) == list(roulette.WEAPONS)

    def test_filters_to_what_the_predicted_credits_actually_cover(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        votable = roulette.affordable_weapons(1000)

        assert "classic" in votable      # 0
        assert "ghost" in votable        # 500
        assert "sheriff" in votable      # 800
        assert "marshal" in votable      # 950
        assert "spectre" not in votable  # 1600
        assert "vandal" not in votable   # 2900
        assert "operator" not in votable # 4700

    def test_a_weapon_priced_exactly_at_the_prediction_is_affordable(self, monkeypatch):
        """Boundary, and a real one - buying with exactly enough creds is a buy, not a near miss."""
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        assert "vandal" in roulette.affordable_weapons(roulette.WEAPON_CREDS_COSTS["vandal"])
        assert "vandal" not in roulette.affordable_weapons(roulette.WEAPON_CREDS_COSTS["vandal"] - 10)

    def test_zero_predicted_credits_still_leaves_the_free_classic_votable(self, monkeypatch):
        """
        A real eco round, not an edge case: 0 creds is a state credit_ocr
        has its own fixture for. The wheel must still have something on it.
        """
        monkeypatch.setattr(config, "_data", {})
        assert roulette.affordable_weapons(0) == ["classic"]

    def test_preserves_the_rosters_own_ordering_rather_than_reordering_by_price(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        votable = roulette.affordable_weapons(3000)
        assert votable == [w for w in roulette.WEAPONS if w in votable]

    def test_the_filter_can_be_switched_off_entirely_in_config(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_affordability_filter_enabled": False})
        assert roulette.affordable_weapons(0) == list(roulette.WEAPONS)

    def test_a_config_override_beats_the_built_in_price(self, monkeypatch):
        """
        Riot retunes weapon prices between patches, so this has to be
        fixable without a deploy.
        """
        monkeypatch.setattr(
            config, "_data", {**NO_RESERVE, "roulette_weapon_creds_costs": {"operator": 100}}
        )
        assert roulette.creds_cost_for("operator") == 100
        assert "operator" in roulette.affordable_weapons(100)

    def test_an_override_leaves_every_other_weapons_price_alone(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_weapon_creds_costs": {"operator": 100}})
        assert roulette.creds_cost_for("vandal") == roulette.WEAPON_CREDS_COSTS["vandal"]

    def test_a_misconfigured_table_that_affords_nothing_falls_back_to_the_full_roster(self, monkeypatch):
        """
        Fails open, like every other path here. An empty votable list would
        mean opening a session nobody can vote in while still charging the
        trigger cost.
        """
        monkeypatch.setattr(config, "_data", {
            "roulette_weapon_creds_costs": {w: 99999 for w in roulette.WEAPONS},
        })
        assert roulette.affordable_weapons(1000) == list(roulette.WEAPONS)

    def test_every_weapon_in_the_roster_has_a_price(self):
        """
        Guards the two lists drifting apart - a weapon added to WEAPONS
        without a price would otherwise fall through to the 0 default and
        be silently votable on an eco round.
        """
        assert set(roulette.WEAPON_CREDS_COSTS) == set(roulette.WEAPONS)


class TestAffordabilityDuringASession:
    @pytest.mark.asyncio
    async def test_trigger_snapshots_the_votable_set_and_only_weights_those(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: 1000)

        await roulette.trigger_roulette("someviewer")

        assert roulette._state.predicted_credits == 1000
        assert "operator" not in roulette._state.votable_weapons
        # The weights dict is what end_roulette() picks a winner from, so an
        # unaffordable weapon must not be in it either.
        assert "operator" not in roulette._state.weights
        assert "ghost" in roulette._state.weights

    @pytest.mark.asyncio
    async def test_the_started_broadcast_carries_the_prediction_and_the_prices(self, monkeypatch):
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: 1000)

        await roulette.trigger_roulette("someviewer")

        payload = mock_broadcast.call_args[0][0]
        assert payload["predicted_credits"] == 1000
        assert "operator" not in payload["weapons"]
        assert payload["weapon_creds_costs"]["ghost"] == 500
        # Only priced for what is on the wheel - no point sending prices for
        # options nobody can pick.
        assert set(payload["weapon_creds_costs"]) == set(payload["weapons"])

    @pytest.mark.asyncio
    async def test_rejects_a_vote_for_an_unaffordable_weapon_without_charging_points(self, monkeypatch):
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        roulette._state.is_active = True
        roulette._state.predicted_credits = 1000
        roulette._state.votable_weapons = roulette.affordable_weapons(1000)
        roulette._state.weights = {w: 0 for w in roulette._state.votable_weapons}

        result = await roulette.vote("someviewer", "operator")

        assert result["ok"] is False
        assert "4700" in result["reason"]
        mock_spend.assert_not_called()
        assert "operator" not in roulette._state.weights

    @pytest.mark.asyncio
    async def test_an_unaffordable_vote_is_reported_separately_from_an_invalid_one(self, monkeypatch):
        """
        "You can't afford that next round" and "that isn't a weapon" are
        different messages for the viewer, so they get different event
        types rather than sharing invalid_vote.
        """
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        roulette._state.is_active = True
        roulette._state.predicted_credits = 1000
        roulette._state.votable_weapons = roulette.affordable_weapons(1000)
        roulette._state.weights = {w: 0 for w in roulette._state.votable_weapons}

        await roulette.vote("someviewer", "operator")

        payload = mock_broadcast.call_args[0][0]
        assert payload["type"] == "unaffordable_vote"
        assert payload["attempted"] == "operator"
        assert payload["creds_cost"] == 4700
        assert payload["predicted_credits"] == 1000

    @pytest.mark.asyncio
    async def test_an_affordable_weapon_still_votes_normally(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "_data",
            {**NO_RESERVE, "roulette_weapon_base_costs": {}, "roulette_vote_cost_increment": 25},
        )
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        roulette._state.is_active = True
        roulette._state.predicted_credits = 1000
        roulette._state.votable_weapons = roulette.affordable_weapons(1000)
        roulette._state.weights = {w: 0 for w in roulette._state.votable_weapons}

        result = await roulette.vote("someviewer", "ghost")

        assert result["ok"] is True
        assert roulette._state.weights["ghost"] == 1

    @pytest.mark.asyncio
    async def test_a_later_ocr_reading_cannot_invalidate_a_vote_mid_session(self, monkeypatch):
        """
        The reason the votable set is snapshotted at trigger time instead
        of recomputed per vote. The OCR window keeps filling while voting
        runs, and its consensus only ever drops - so a live lookup could
        pull a weapon off the wheel that viewers were shown, after some of
        them had already paid points for it.
        """
        monkeypatch.setattr(config, "_data", dict(NO_RESERVE))
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: 3000)

        await roulette.trigger_roulette("someviewer")
        assert "vandal" in roulette._state.votable_weapons

        # Buy phase continues, a lower reading lands, consensus drops.
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: 200)

        result = await roulette.vote("someviewer", "vandal")
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_an_unrecognized_weapon_is_still_invalid_not_unaffordable(self, monkeypatch):
        """The two rejections must not blur into each other once both exist."""
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        roulette._state.is_active = True
        roulette._state.votable_weapons = roulette.affordable_weapons(1000)
        roulette._state.weights = {w: 0 for w in roulette._state.votable_weapons}

        result = await roulette.vote("someviewer", "not_a_real_gun")

        assert "isn't a recognized weapon" in result["reason"].lower()
        assert mock_broadcast.call_args[0][0]["type"] == "invalid_vote"


class TestVote:
    @pytest.mark.asyncio
    async def test_rejects_a_vote_when_no_session_is_active(self, monkeypatch):
        result = await roulette.vote("someviewer", "vandal")
        assert result["ok"] is False
        assert "no roulette" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_rejects_an_unrecognized_weapon(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}

        result = await roulette.vote("someviewer", "not_a_real_gun")

        assert result["ok"] is False
        assert "isn't a recognized weapon" in result["reason"].lower()

    @pytest.mark.asyncio
    async def test_successful_vote_increments_weight_and_deducts_points(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_vote_cost_increment": 25})
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.vote("someviewer", "VANDAL")  # case-insensitivity check too

        assert result["ok"] is True
        assert roulette._state.weights["vandal"] == 1
        mock_spend.assert_called_once_with(
            "someviewer", roulette.DEFAULT_VOTE_BASE_COST, platform="twitch"
        )

    @pytest.mark.asyncio
    async def test_cost_escalates_with_each_subsequent_vote_on_the_same_weapon(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_vote_cost_increment": 25})
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette.vote("a", "vandal")
        await roulette.vote("b", "vandal")
        await roulette.vote("c", "vandal")

        costs_charged = [call.args[1] for call in mock_spend.call_args_list]
        assert costs_charged == [50, 75, 100]  # base 50, then +25 each time

    @pytest.mark.asyncio
    async def test_concurrent_votes_on_the_same_weapon_still_escalate_correctly(self, monkeypatch):
        """
        The actual race condition found and fixed: if cost calculation and
        weight increment aren't part of the same atomic sequence, two
        near-simultaneous votes could both compute the same (wrong,
        non-escalated) cost. This forces genuine interleaving via asyncio.gather.
        """
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_vote_cost_increment": 25})

        mock_spend = AsyncMock(return_value=(True, None))

        async def slow_spend(username, amount, platform="twitch"):
            await asyncio.sleep(0)  # yield control, encouraging real interleaving
            return await mock_spend(username, amount, platform=platform)

        monkeypatch.setattr(roulette, "try_spend", slow_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await asyncio.gather(
            roulette.vote("a", "vandal"),
            roulette.vote("b", "vandal"),
            roulette.vote("c", "vandal"),
        )

        costs_charged = sorted(call.args[1] for call in mock_spend.call_args_list)
        assert costs_charged == [50, 75, 100]  # still correctly escalated, no duplicates
        assert roulette._state.weights["vandal"] == 3

    @pytest.mark.asyncio
    async def test_rejects_insufficient_balance_without_incrementing_weight(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(False, 10)))

        result = await roulette.vote("brokeviewer", "operator")

        assert result["ok"] is False
        assert "10" in result["reason"]
        assert roulette._state.weights["operator"] == 0


class TestEndRoulette:
    @pytest.mark.asyncio
    async def test_draws_from_the_whole_roster_not_just_the_top_vote(self, monkeypatch):
        """
        A vote is a thumb on the scale, not an elimination. The first vote
        used to decide the round outright while the other seventeen
        weapons stayed listed and votable but could no longer win.
        """
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["phantom"] = 5
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        winner = await roulette.end_roulette()

        assert winner in roulette.WEAPONS
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_an_unvoted_session_still_draws_a_winner(self, monkeypatch):
        """
        Nobody voting means every weapon carries the same weight, and a
        field of equal weights still has an outcome. Returning None here
        made the trigger cost buy silence - no forced buy, no result on
        the overlay, and no reason to pay it again on a quiet chat.
        """
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())

        winner = await roulette.end_roulette()

        assert winner in roulette.WEAPONS

    @pytest.mark.asyncio
    async def test_the_random_pick_only_draws_from_this_session_roster(self, monkeypatch):
        """
        The draw has to respect the affordability snapshot the same way a
        vote does - offering a weapon nobody could have voted for is the
        exact failure the filter exists to prevent.
        """
        roulette._state.is_active = True
        roulette._state.weights = {"classic": 0, "ghost": 0, "sheriff": 0}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())

        for _ in range(30):
            roulette._state.is_active = True
            assert await roulette.end_roulette() in ("classic", "ghost", "sheriff")

    @pytest.mark.asyncio
    async def test_an_unvoted_session_still_queues_the_forced_buy(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        mock_forced_buy = AsyncMock()
        monkeypatch.setattr(roulette, "_start_forced_buy", mock_forced_buy)

        winner = await roulette.end_roulette()

        mock_forced_buy.assert_awaited_once_with(winner)

    @pytest.mark.asyncio
    async def test_the_overlay_is_told_the_winner_was_not_voted_for(self, monkeypatch):
        """
        A uniform draw and a vote result are different things, and a
        viewer should not be shown a winner that looks earned when nobody
        picked it.
        """
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())

        await roulette.end_roulette()

        assert mock_broadcast.await_args[0][0]["randomly_picked"] is True

    @pytest.mark.asyncio
    async def test_a_real_vote_result_is_never_marked_as_a_random_pick(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {"vandal": 3, "phantom": 1}
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        winner = await roulette.end_roulette()

        assert winner in ("vandal", "phantom")
        assert mock_broadcast.await_args[0][0]["randomly_picked"] is False

    @pytest.mark.asyncio
    async def test_the_random_pick_can_be_switched_off(self, monkeypatch):
        """The old behaviour, kept reachable rather than deleted."""
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_random_pick_when_no_votes": False})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        assert await roulette.end_roulette() is None

    @pytest.mark.asyncio
    async def test_ending_an_already_inactive_session_is_a_safe_no_op(self):
        winner = await roulette.end_roulette()
        assert winner is None


    @pytest.mark.asyncio
    async def test_announces_the_winner_in_chat(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.platform = "twitch"
        roulette._state.weights = {"vandal": 3, "phantom": 1}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        monkeypatch.setattr(roulette, "draw_winner", lambda shares: "vandal")

        assert await roulette.end_roulette() == "vandal"
        assert "vandal" in mock_send.await_args[0][0]

    @pytest.mark.asyncio
    async def test_chat_says_the_winner_was_not_voted_for(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        winner = await roulette.end_roulette()

        reply = mock_send.await_args[0][0]
        assert "No votes" in reply
        assert winner in reply

    @pytest.mark.asyncio
    async def test_chat_still_reports_nothing_happening_when_the_draw_is_off(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_random_pick_when_no_votes": False})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        assert await roulette.end_roulette() is None
        assert "no votes" in mock_send.await_args[0][0]

    @pytest.mark.asyncio
    async def test_the_winner_announcement_goes_to_the_triggering_platform(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.platform = "youtube"
        roulette._state.weights = {"vandal": 2}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.end_roulette()

        assert mock_send.await_args[1]["platform"] == "youtube"


class TestForcedBuyBadge:
    @pytest.mark.asyncio
    async def test_ending_with_a_winner_starts_the_forced_buy_as_queued(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["vandal"] = 3
        monkeypatch.setattr(config, "_data", {"forced_buy_queued_duration_seconds": 9999})  # long, so it stays "queued" for this test
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        # The draw is weighted, not deterministic - pinned here so this
        # test is about the badge, not about which weapon came up.
        monkeypatch.setattr(roulette, "draw_winner", lambda shares: "vandal")

        await roulette.end_roulette()

        assert roulette._state.forced_buy_weapon == "vandal"
        assert roulette._state.forced_buy_phase == "queued"
        broadcast_types = [call.args[0]["type"] for call in mock_broadcast.call_args_list]
        assert "forced_buy_queued" in broadcast_types

    @pytest.mark.asyncio
    async def test_no_winner_means_no_forced_buy_started(self, monkeypatch):
        # An unvoted session normally draws a winner and DOES queue a
        # forced buy (see TestEndRoulette) - so the only way to reach the
        # no-winner path now is with that draw switched off.
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}  # nobody voted
        monkeypatch.setattr(config, "_data", {"roulette_random_pick_when_no_votes": False})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette.end_roulette()

        assert roulette._state.forced_buy_weapon is None

    @pytest.mark.asyncio
    async def test_the_first_new_buy_phase_makes_the_badge_active(self, monkeypatch):
        """
        The buy phase after a win is the one the weapon actually gets
        bought in - a real signal for what the queued->active timer can
        only approximate.
        """
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        roulette._state.forced_buy_weapon = "vandal"
        roulette._state.forced_buy_phase = "queued"

        await roulette.on_new_buy_phase()

        assert roulette._state.forced_buy_phase == "active"
        assert roulette._state.forced_buy_weapon == "vandal"

    @pytest.mark.asyncio
    async def test_the_second_new_buy_phase_clears_the_badge(self, monkeypatch):
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        roulette._state.forced_buy_weapon = "vandal"
        roulette._state.forced_buy_phase = "queued"

        await roulette.on_new_buy_phase()
        await roulette.on_new_buy_phase()

        assert roulette._state.forced_buy_weapon is None
        assert roulette._state.forced_buy_phase is None

    @pytest.mark.asyncio
    async def test_a_timer_that_already_promoted_the_badge_cannot_shift_the_count(self, monkeypatch):
        """
        The fallback timer can flip queued->active on its own while
        nothing is happening. If the signal read its meaning off
        forced_buy_phase, the very next buy phase - the one the weapon is
        bought in - would clear the badge instead of activating it.
        """
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        roulette._state.forced_buy_weapon = "vandal"
        roulette._state.forced_buy_phase = "active"  # timer got there first
        roulette._state.forced_buy_phases_seen = 0

        await roulette.on_new_buy_phase()

        assert roulette._state.forced_buy_weapon == "vandal"

    @pytest.mark.asyncio
    async def test_a_new_buy_phase_with_no_badge_showing_is_a_no_op(self, monkeypatch):
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.on_new_buy_phase()

        mock_broadcast.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_signal_cancels_the_fallback_timer(self, monkeypatch):
        """
        Both are trying to do the same job. If the timer still fired
        afterwards it would advance the badge a second time.
        """
        monkeypatch.setattr(
            config,
            "_data",
            {"forced_buy_queued_duration_seconds": 0.02, "forced_buy_active_duration_seconds": 5},
        )
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette._start_forced_buy("vandal")
        await roulette.on_new_buy_phase()   # beats the queued->active timer
        await asyncio.sleep(0.05)           # ...which would have fired by now

        assert roulette._state.forced_buy_phase == "active"
        assert roulette._state.forced_buy_weapon == "vandal"

    @pytest.mark.asyncio
    async def test_the_badge_clears_itself_once_the_round_is_over(self, monkeypatch):
        """
        "active" used to be terminal - clear_forced_buy() was only ever
        called by the NEXT roulette starting, so the badge sat on stream
        announcing a gun that had stopped being in play rounds ago.
        """
        monkeypatch.setattr(
            config,
            "_data",
            {
                "forced_buy_queued_duration_seconds": 0.01,
                "forced_buy_active_duration_seconds": 0.01,
            },
        )
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette._start_forced_buy("vandal")
        await asyncio.sleep(0.05)

        assert roulette._state.forced_buy_weapon is None
        assert roulette._state.forced_buy_phase is None
        types = [call.args[0]["type"] for call in mock_broadcast.await_args_list]
        assert types == ["forced_buy_queued", "forced_buy_active", "forced_buy_cleared"]

    @pytest.mark.asyncio
    async def test_a_stale_clear_cannot_drop_a_newer_badge(self, monkeypatch):
        """
        The clear is a delayed task. If a new roulette produced its own
        forced buy in the meantime, that one must survive.
        """
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())
        roulette._state.forced_buy_weapon = "phantom"
        roulette._state.forced_buy_phase = "queued"

        await roulette._clear_forced_buy_after_delay("vandal", 0)

        assert roulette._state.forced_buy_weapon == "phantom"

    @pytest.mark.asyncio
    async def test_transitions_from_queued_to_active_after_the_configured_delay(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"forced_buy_queued_duration_seconds": 0.01})
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette._start_forced_buy("vandal")
        assert roulette._state.forced_buy_phase == "queued"

        await asyncio.sleep(0.05)  # let the delayed task actually run

        assert roulette._state.forced_buy_phase == "active"
        broadcast_types = [call.args[0]["type"] for call in mock_broadcast.call_args_list]
        assert "forced_buy_active" in broadcast_types

    @pytest.mark.asyncio
    async def test_a_new_roulette_clears_a_stale_previous_forced_buy(self, monkeypatch):
        roulette._state.forced_buy_weapon = "phantom"
        roulette._state.forced_buy_phase = "active"
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.trigger_roulette("someviewer")

        assert roulette._state.forced_buy_weapon is None
        broadcast_types = [call.args[0]["type"] for call in mock_broadcast.call_args_list]
        assert "forced_buy_cleared" in broadcast_types

    @pytest.mark.asyncio
    async def test_clearing_when_nothing_was_active_does_not_broadcast_unnecessarily(self, monkeypatch):
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.clear_forced_buy()

        mock_broadcast.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_stale_delayed_activation_does_not_overwrite_a_newer_forced_buy(self, monkeypatch):
        """
        Guards against a real edge case: if roulette #1's forced buy is
        still in its queued delay when roulette #2 ALSO produces a winner,
        roulette #1's delayed task must not incorrectly activate and
        overwrite roulette #2's newer, unrelated result.
        """
        monkeypatch.setattr(config, "_data", {"forced_buy_queued_duration_seconds": 0.01})
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette._start_forced_buy("vandal")  # roulette #1's result
        roulette._state.forced_buy_weapon = "phantom"  # roulette #2 has since taken over
        roulette._state.forced_buy_phase = "queued"

        await asyncio.sleep(0.05)  # let roulette #1's stale delayed task run

        assert roulette._state.forced_buy_weapon == "phantom"
        assert roulette._state.forced_buy_phase == "queued"  # NOT incorrectly flipped to "active" by the stale task


class TestHandleChatCommand:
    def make_chat_event(self, username: str, message: str, source: str = "Twitch") -> dict:
        """The current Streamer.bot Twitch shape - `data.user` plus
        `data.text`. The older nested-message shape this backend was first
        written against is covered in test_streamerbot_client.py, since
        both go through the same parse_chat_message()."""
        return {
            "event": {"source": source, "type": "ChatMessage"},
            "data": {"user": {"login": username, "name": username}, "text": message},
        }

    @pytest.mark.asyncio
    async def test_roulette_command_triggers_a_session(self, monkeypatch):
        mock_trigger = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "trigger_roulette", mock_trigger)
        monkeypatch.setattr(roulette, "_reply_in_chat", AsyncMock())

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        mock_trigger.assert_called_once_with("someviewer", platform="twitch")

    @pytest.mark.asyncio
    async def test_weapon_command_casts_a_vote(self, monkeypatch):
        mock_vote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "vote", mock_vote)
        monkeypatch.setattr(roulette, "_reply_in_chat", AsyncMock())

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!vandal"))

        mock_vote.assert_called_once_with("someviewer", "vandal", platform="twitch")

    @pytest.mark.asyncio
    async def test_ignores_messages_that_are_not_commands(self, monkeypatch):
        mock_trigger = AsyncMock(return_value={"ok": True})
        mock_vote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "trigger_roulette", mock_trigger)
        monkeypatch.setattr(roulette, "vote", mock_vote)
        monkeypatch.setattr(roulette, "_reply_in_chat", AsyncMock())

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "gg that was close"))

        mock_trigger.assert_not_called()
        mock_vote.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_chat_message_events(self, monkeypatch):
        mock_trigger = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "trigger_roulette", mock_trigger)

        await roulette.handle_chat_command({"event": {"type": "Follow"}, "data": {}})

        mock_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_unrecognized_commands_silently_when_no_session_is_active(self, monkeypatch):
        """
        With no roulette running, an unrelated "!word" (e.g. some other
        bot's !discord or !lurk command) should never be treated as a
        vote attempt at all - there's nothing to vote on, so silently
        ignoring it avoids incorrectly flagging unrelated commands.
        """
        mock_vote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "vote", mock_vote)
        monkeypatch.setattr(roulette, "_reply_in_chat", AsyncMock())

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!notarealgunorcommand"))

        mock_vote.assert_not_called()

    @pytest.mark.asyncio
    async def test_attempts_a_vote_for_unrecognized_weapons_while_a_session_is_active(self, monkeypatch):
        """
        The actual gap raised: while a roulette IS active, an unrecognized
        weapon name should still reach vote() so its own validation can
        give real feedback, rather than silently vanishing with nothing
        shown to the person who tried.
        """
        roulette._state.is_active = True
        mock_vote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "vote", mock_vote)
        monkeypatch.setattr(roulette, "_reply_in_chat", AsyncMock())

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!notarealgun"))

        mock_vote.assert_called_once_with("someviewer", "notarealgun", platform="twitch")

    @pytest.mark.asyncio
    async def test_broadcasts_invalid_vote_feedback_for_an_unrecognized_weapon(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        result = await roulette.vote("someviewer", "not_a_real_gun")

        assert result["ok"] is False
        mock_broadcast.assert_called_once()
        payload, kwargs = mock_broadcast.call_args
        assert payload[0]["type"] == "invalid_vote"
        assert payload[0]["attempted"] == "not_a_real_gun"
        assert kwargs["tag"] == "roulette"

    @pytest.mark.asyncio
    async def test_answers_the_viewer_in_chat_when_a_trigger_is_refused(self, monkeypatch):
        """
        The whole point of this path: before it existed, every refusal
        reason was computed and then thrown away, so somebody who typed
        !roulette without enough points saw nothing at all happen and had
        no way to tell that from the bot being down.
        """
        monkeypatch.setattr(
            roulette, "trigger_roulette", AsyncMock(return_value={"ok": False, "reason": "Need 500 points, you have 20"})
        )
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        mock_send.assert_awaited_once()
        text, kwargs = mock_send.await_args
        assert text[0] == "@someviewer Need 500 points, you have 20"
        assert kwargs["platform"] == "twitch"

    @pytest.mark.asyncio
    async def test_answers_the_viewer_in_chat_when_a_vote_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            roulette, "vote", AsyncMock(return_value={"ok": False, "reason": "operator costs 4700 creds"})
        )
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!operator"))

        assert mock_send.await_args[0][0] == "@someviewer operator costs 4700 creds"

    @pytest.mark.asyncio
    async def test_stays_silent_on_a_successful_vote(self, monkeypatch):
        """One chat line per vote would drown the channel during a busy
        window - the overlay already shows the vote landing."""
        monkeypatch.setattr(roulette, "vote", AsyncMock(return_value={"ok": True, "new_weight": 3}))
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!vandal"))

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_replies_go_back_to_the_platform_the_command_came_from(self, monkeypatch):
        monkeypatch.setattr(
            roulette, "trigger_roulette", AsyncMock(return_value={"ok": False, "reason": "Roulette is on cooldown"})
        )
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        event = self.make_chat_event("someviewer", "!roulette", source="YouTube")
        await roulette.handle_chat_command(event)

        assert mock_send.await_args[1]["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_chat_replies_can_be_switched_off(self, monkeypatch):
        """SendMessage is the one request Streamer.bot documents as needing
        authentication on its WebSocket server; if that is enabled at the
        gaming PC end, silence beats a rejection logged per command."""
        monkeypatch.setattr(config, "_data", {"roulette_chat_replies_enabled": False})
        monkeypatch.setattr(
            roulette, "trigger_roulette", AsyncMock(return_value={"ok": False, "reason": "nope"})
        )
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_announces_the_open_session_with_its_real_roster_size(self, monkeypatch):
        monkeypatch.setattr(roulette, "trigger_roulette", AsyncMock(return_value={"ok": True}))
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)
        monkeypatch.setattr(config, "_data", {"roulette_voting_duration_seconds": 18})
        roulette._state.votable_weapons = ["classic", "shorty", "frenzy", "ghost", "sheriff"]
        roulette._state.predicted_credits = 900

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        announcement = mock_send.await_args[0][0]
        assert "5 weapons available" in announcement
        assert "900 creds next round" in announcement
        assert "18s" in announcement

    @pytest.mark.asyncio
    async def test_announcement_says_so_when_there_is_no_credit_reading(self, monkeypatch):
        monkeypatch.setattr(roulette, "trigger_roulette", AsyncMock(return_value={"ok": True}))
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)
        roulette._state.votable_weapons = list(roulette.WEAPONS)
        roulette._state.predicted_credits = None

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        announcement = mock_send.await_args[0][0]
        assert "every weapon is in play" in announcement
        assert f"{len(roulette.WEAPONS)} weapons available" in announcement

    @pytest.mark.asyncio
    async def test_help_command_replies_with_trigger_command_and_full_weapon_list(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!help"))

        reply = mock_send.await_args[0][0]
        assert "!roulette" in reply
        for weapon in roulette.WEAPONS:
            assert weapon in reply

    @pytest.mark.asyncio
    async def test_the_help_reply_is_not_itself_command_shaped(self, monkeypatch):
        """
        Replies come back down the subscription as ordinary chat events.
        This one opened with "!roulette", so answering !help parsed as a
        !roulette trigger and charged the asker for a session they never
        asked for. streamerbot_client drops echoes of our own messages
        now; this is the second lock on the same door.
        """
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!help"))

        assert not mock_send.await_args[0][0].startswith("!")

    @pytest.mark.asyncio
    async def test_commands_is_an_alias_for_help(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!commands"))

        assert "!roulette" in mock_send.await_args[0][0]

    @pytest.mark.asyncio
    async def test_help_works_even_with_no_session_active(self, monkeypatch):
        """Unlike a bare weapon-shaped word, !help must never be swallowed
        by the mistaken-vote catch-all - it's the one command a viewer
        needs whether or not a roulette happens to be running."""
        mock_vote = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(roulette, "vote", mock_vote)
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)
        assert roulette._state.is_active is False

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!help"))

        mock_vote.assert_not_called()
        mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_help_reports_the_configured_trigger_cost(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", mock_send)
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 750})

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!help"))

        assert "750" in mock_send.await_args[0][0]


class TestTooPoorMessage:
    """
    What a viewer is told when they can't afford something. The balance is
    optional because not every backend can report one - the cloudbot
    backend only learns a balance when a spend comes back clamped.
    """

    def test_names_the_balance_when_the_backend_knows_it(self):
        assert roulette._too_poor(500, 120) == "Need 500 points, you have 120"

    def test_leaves_the_number_out_when_it_is_unknown(self):
        """"you have 0" would be a guess, and a discouraging one."""
        assert roulette._too_poor(500, None) == "Need 500 points"

    def test_zero_is_reported_as_zero_not_as_unknown(self):
        assert roulette._too_poor(500, 0) == "Need 500 points, you have 0"

    @pytest.mark.asyncio
    async def test_a_trigger_refused_without_a_known_balance_still_names_the_cost(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 500})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(False, None)))

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is False
        assert result["reason"] == "Need 500 points"
        assert roulette._state.is_active is False


class TestUnknownUserRefusal:
    """
    Cloudbot keeps a separate wallet per platform and can only be asked
    about users on `cloudbot_platform`, so a viewer who only ever chats on
    the other one comes back as unknown. That is the one failure here they
    can fix themselves, so it must not read as a generic outage.
    """

    @pytest.mark.asyncio
    async def test_a_trigger_says_where_the_points_live(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_platform": "twitch"})
        monkeypatch.setattr(
            roulette, "try_spend", AsyncMock(side_effect=roulette.UnknownUser("nope"))
        )

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is False
        assert "no record of you" in result["reason"].lower()
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_a_vote_says_the_same_thing(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_platform": "twitch"})
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(
            roulette, "try_spend", AsyncMock(side_effect=roulette.UnknownUser("nope"))
        )

        result = await roulette.vote("someviewer", "vandal")

        assert result["ok"] is False
        assert "no record of you" in result["reason"].lower()
        assert roulette._state.weights["vandal"] == 0

    @pytest.mark.asyncio
    async def test_a_real_outage_still_reads_as_a_retry(self, monkeypatch):
        """A timeout is not the viewer's fault and not theirs to fix."""
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(
            roulette, "try_spend", AsyncMock(side_effect=TimeoutError("no answer"))
        )

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is False
        assert "try again" in result["reason"].lower()
        assert "no record" not in result["reason"].lower()


class TestUnreachableWallet:
    """
    Every platform is charged by default, because the spend goes to the
    viewer's own chat - Cloudbot resolves a username only in the chat the
    command was typed in. An earlier version refused every non-Twitch
    viewer outright, on evidence that was really about YouTube spends
    being sent to Twitch chat.

    `cloudbot_platforms` is the way to switch one back off without a
    deploy, if it turns out Cloudbot genuinely can't serve it.
    """

    @pytest.mark.asyncio
    async def test_youtube_is_charged_like_anywhere_else_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.trigger_roulette("someviewer", platform="youtube")

        assert result["ok"] is True
        assert mock_spend.await_args.kwargs["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_a_vote_charges_in_the_chat_it_came_from(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.vote("someviewer", "vandal", platform="youtube")

        assert result["ok"] is True
        assert mock_spend.await_args.kwargs["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_a_platform_can_be_switched_off_by_config(self, monkeypatch):
        monkeypatch.setattr(
            config, "_data", {"points_backend": "cloudbot", "cloudbot_platforms": ["twitch"]}
        )
        mock_spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(roulette, "try_spend", mock_spend)

        result = await roulette.trigger_roulette("someviewer", platform="youtube")

        assert result["ok"] is False
        assert "twitch" in result["reason"].lower()
        mock_spend.assert_not_called()
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_the_allowlist_never_applies_to_the_other_backends(self, monkeypatch):
        """Only Cloudbot keeps a separate wallet per platform."""
        monkeypatch.setattr(
            config, "_data", {"points_backend": "local", "cloudbot_platforms": ["twitch"]}
        )
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.trigger_roulette("someviewer", platform="youtube")

        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_a_mistyped_word_still_reads_as_a_typo_not_a_wallet_problem(self, monkeypatch):
        """The guard sits with the payment, after the "is that a weapon?" checks."""
        monkeypatch.setattr(
            config, "_data", {"points_backend": "cloudbot", "cloudbot_platforms": ["twitch"]}
        )
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.vote("someviewer", "aries", platform="youtube")

        assert "isn't a recognized weapon" in result["reason"]


class TestWheelShares:
    """
    A vote buys more room on the wheel, not the round. Every votable
    weapon keeps a slice, so seventeen unvoted weapons are still live
    after somebody votes for the eighteenth - which is what a viewer
    paying 50 points for "more chance" is buying.
    """

    def test_every_votable_weapon_has_a_slice_before_anyone_votes(self):
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        shares = roulette.wheel_shares()

        assert set(shares) == set(roulette.WEAPONS)
        assert set(shares.values()) == {1}

    def test_one_vote_doubles_that_weapon_and_removes_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["vandal"] = 1

        shares = roulette.wheel_shares()

        assert shares["vandal"] == 2
        assert shares["phantom"] == 1
        assert len(shares) == len(roulette.WEAPONS)

    def test_only_the_session_roster_is_on_the_wheel(self):
        """An unaffordable weapon was never votable, so it isn't drawable."""
        roulette._state.weights = {"classic": 0, "ghost": 2}

        assert set(roulette.wheel_shares()) == {"classic", "ghost"}

    def test_the_base_share_is_configurable(self, monkeypatch):
        """Raise it to flatten the odds, lower it to make votes decisive."""
        monkeypatch.setattr(config, "_data", {"roulette_base_wheel_share": 5})
        roulette._state.weights = {"vandal": 1, "phantom": 0}

        assert roulette.wheel_shares() == {"vandal": 6, "phantom": 5}

    def test_a_zero_base_restores_the_old_winner_takes_all(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_base_wheel_share": 0})
        roulette._state.weights = {"vandal": 1, "phantom": 0}

        assert roulette.draw_winner(roulette.wheel_shares()) == "vandal"


class TestDrawWinner:
    def test_an_empty_wheel_has_no_winner(self):
        assert roulette.draw_winner({}) is None

    def test_a_wheel_of_nothing_has_no_winner(self):
        """Reachable with roulette_base_wheel_share at 0 and no votes."""
        assert roulette.draw_winner({"vandal": 0, "phantom": 0}) is None

    def test_a_weapon_with_no_share_never_wins(self):
        assert roulette.draw_winner({"vandal": 1, "phantom": 0}) == "vandal"

    def test_the_heavier_weapon_wins_more_often(self):
        """
        Statistical, so it is pinned to a seed - the point is that weight
        biases the draw, not that any single spin goes one way.
        """
        random.seed(1234)
        shares = {"vandal": 9, "phantom": 1}
        wins = [roulette.draw_winner(shares) for _ in range(2000)]

        assert 0.8 < wins.count("vandal") / len(wins) < 0.98

    def test_an_even_wheel_is_roughly_even(self):
        random.seed(1234)
        shares = {w: 1 for w in ("vandal", "phantom", "ghost", "sheriff")}
        wins = [roulette.draw_winner(shares) for _ in range(2000)]

        for weapon in shares:
            assert 0.2 < wins.count(weapon) / len(wins) < 0.3

    @pytest.mark.asyncio
    async def test_the_broadcast_carries_the_wheel_it_drew_on(self, monkeypatch):
        """
        So the overlay renders the real odds instead of inferring them
        from vote counts and dropping every weapon still at zero.
        """
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["vandal"] = 2
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)
        monkeypatch.setattr(roulette, "_start_forced_buy", AsyncMock())
        monkeypatch.setattr(roulette.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        await roulette.end_roulette()

        ended = mock_broadcast.await_args[0][0]
        assert ended["wheel_shares"]["vandal"] == 3
        assert ended["wheel_shares"]["phantom"] == 1
        assert len(ended["wheel_shares"]) == len(roulette.WEAPONS)


class TestRefunds:
    """
    Points are spent before the thing they pay for exists - they have to
    be, or somebody who can't pay could start a session. That leaves a
    window where the money is gone and the roulette isn't running, and
    anything failing in it has to put the points back.
    """

    @pytest.mark.asyncio
    async def test_a_trigger_that_cannot_open_refunds_the_cost(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 500})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(
            roulette.widget_hub, "broadcast", AsyncMock(side_effect=RuntimeError("hub is down"))
        )
        mock_grant = AsyncMock(return_value=None)
        monkeypatch.setattr(roulette, "grant_points", mock_grant)

        result = await roulette.trigger_roulette("someviewer", platform="youtube")

        assert result["ok"] is False
        assert "points are back" in result["reason"]
        mock_grant.assert_awaited_once_with("someviewer", 500, platform="youtube")

    @pytest.mark.asyncio
    async def test_a_failed_trigger_leaves_no_half_started_session(self, monkeypatch):
        """Otherwise the next !roulette is refused as 'already in progress'."""
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(
            roulette.widget_hub, "broadcast", AsyncMock(side_effect=RuntimeError("hub is down"))
        )
        monkeypatch.setattr(roulette, "grant_points", AsyncMock(return_value=None))

        await roulette.trigger_roulette("someviewer")

        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_a_refund_that_fails_does_not_hide_the_original_error(self, monkeypatch):
        """Already the error path - a second failure must not replace the first."""
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(
            roulette.widget_hub, "broadcast", AsyncMock(side_effect=RuntimeError("hub is down"))
        )
        monkeypatch.setattr(
            roulette, "grant_points", AsyncMock(side_effect=RuntimeError("cloudbot is down"))
        )

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_a_vote_that_lands_after_the_window_closed_is_refunded(self, monkeypatch):
        """
        The spend is a chat round trip of up to a couple of seconds, and
        the window can close inside it - so the vote arrives at a session
        that has already been drawn.
        """
        monkeypatch.setattr(config, "_data", {})
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        mock_grant = AsyncMock(return_value=None)
        monkeypatch.setattr(roulette, "grant_points", mock_grant)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        async def spend_then_close(username, amount, platform=""):
            roulette._state.is_active = False
            return True, None

        monkeypatch.setattr(roulette, "try_spend", spend_then_close)

        result = await roulette.vote("someviewer", "vandal", platform="twitch")

        assert result["ok"] is False
        assert "points are back" in result["reason"]
        mock_grant.assert_awaited_once_with(
            "someviewer", roulette.DEFAULT_VOTE_BASE_COST, platform="twitch"
        )
        assert roulette._state.weights["vandal"] == 0

    @pytest.mark.asyncio
    async def test_a_normal_vote_is_not_refunded(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        mock_grant = AsyncMock(return_value=None)
        monkeypatch.setattr(roulette, "grant_points", mock_grant)
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.vote("someviewer", "vandal")

        assert result["ok"] is True
        mock_grant.assert_not_called()


class TestReservedCreds:
    """
    Shields and abilities come out of the same wallet as the gun, every
    round. A roster built from the raw reading offers weapons that can't
    actually be bought alongside them - 5000 creds is an Odin OR a Vandal
    plus a full kit, not both.
    """

    def test_a_full_buy_reserves_shield_and_abilities(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert roulette.reserved_creds(5000) == 1400
        assert roulette.spendable_creds(5000) == 3600

    def test_the_odin_stops_being_offered_on_a_5000_cred_round(self, monkeypatch):
        """3200 for the Odin plus 1400 of kit is 4600 - it fits. 5000 minus
        the reserve leaves 3600, which still covers it; the Operator does not."""
        monkeypatch.setattr(config, "_data", {})
        votable = roulette.affordable_weapons(5000)

        assert "odin" in votable       # 3200 <= 3600
        assert "operator" not in votable  # 4700 > 3600, and unbuyable with a kit

    def test_a_vandal_round_is_measured_against_what_is_left(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert "vandal" not in roulette.affordable_weapons(3000)   # 2900 > 1600
        assert "vandal" in roulette.affordable_weapons(4300)       # 2900 <= 2900

    def test_a_pistol_round_reserves_less(self, monkeypatch):
        """Nobody buys heavy shield out of the 800 a pistol round issues."""
        monkeypatch.setattr(config, "_data", {})
        assert roulette.reserved_creds(800) == 400
        assert roulette.spendable_creds(800) == 400

    def test_the_pistol_threshold_is_the_number_not_a_round_counter(self, monkeypatch):
        """There is no round tracking here to ask."""
        monkeypatch.setattr(config, "_data", {})
        assert roulette.reserved_creds(800) == 400
        assert roulette.reserved_creds(810) == 1400

    def test_every_reserve_is_configurable(self, monkeypatch):
        """Riot retunes shield and ability prices between patches."""
        monkeypatch.setattr(
            config,
            "_data",
            {"roulette_shield_reserve_creds": 500, "roulette_ability_reserve_creds": 100},
        )
        assert roulette.reserved_creds(5000) == 600

    def test_the_reserve_never_makes_the_budget_negative(self, monkeypatch):
        """
        A negative budget would make even the free Classic unaffordable,
        which trips the misconfiguration fallback and opens the FULL
        roster - the opposite of what a broke round should show.
        """
        monkeypatch.setattr(config, "_data", {"roulette_pistol_reserved_creds": 5000})

        assert roulette.spendable_creds(800) == 0
        assert roulette.affordable_weapons(800) == ["classic"]

    def test_no_prediction_still_opens_the_whole_roster(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert roulette.spendable_creds(None) is None
        assert roulette.affordable_weapons(None) == list(roulette.WEAPONS)

    @pytest.mark.asyncio
    async def test_the_started_broadcast_carries_both_numbers(self, monkeypatch):
        """The raw reading is what the streamer sees in game; the spendable
        figure is what the roster was actually measured against."""
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(roulette, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(roulette.credit_ocr, "get_predicted_credits", lambda: 5000)
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.trigger_roulette("someviewer")

        payload = mock_broadcast.call_args[0][0]
        assert payload["predicted_credits"] == 5000
        assert payload["spendable_credits"] == 3600
