import sys
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
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=1000))
        monkeypatch.setattr(roulette, "subtract_points", AsyncMock())
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.trigger_roulette("someviewer")

        assert result["ok"] is True
        assert roulette._state.is_active is True
        assert set(roulette._state.weights.keys()) == set(roulette.WEAPONS)
        assert all(w == 0 for w in roulette._state.weights.values())

    @pytest.mark.asyncio
    async def test_rejects_with_insufficient_points_without_deducting_anything(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"roulette_trigger_cost": 500})
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=100))
        mock_subtract = AsyncMock()
        monkeypatch.setattr(roulette, "subtract_points", mock_subtract)

        result = await roulette.trigger_roulette("brokeviewer")

        assert result["ok"] is False
        assert "100" in result["reason"]
        mock_subtract.assert_not_called()
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_rejects_a_second_trigger_while_one_is_already_active(self, monkeypatch):
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=1000))
        monkeypatch.setattr(roulette, "subtract_points", AsyncMock())
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
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=1000))
        monkeypatch.setattr(roulette, "subtract_points", AsyncMock())
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.trigger_roulette("someviewer")

        mock_broadcast.assert_called_once()
        payload, kwargs = mock_broadcast.call_args
        assert payload[0]["type"] == "roulette_started"
        assert set(payload[0]["weapons"]) == set(roulette.WEAPONS)
        assert kwargs["tag"] == "roulette"


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
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=1000))
        mock_subtract = AsyncMock()
        monkeypatch.setattr(roulette, "subtract_points", mock_subtract)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        result = await roulette.vote("someviewer", "VANDAL")  # case-insensitivity check too

        assert result["ok"] is True
        assert roulette._state.weights["vandal"] == 1
        mock_subtract.assert_called_once_with("someviewer", roulette.DEFAULT_VOTE_BASE_COST)

    @pytest.mark.asyncio
    async def test_cost_escalates_with_each_subsequent_vote_on_the_same_weapon(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(config, "_data", {"roulette_vote_cost_increment": 25})
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=10000))
        mock_subtract = AsyncMock()
        monkeypatch.setattr(roulette, "subtract_points", mock_subtract)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette.vote("a", "vandal")
        await roulette.vote("b", "vandal")
        await roulette.vote("c", "vandal")

        costs_charged = [call.args[1] for call in mock_subtract.call_args_list]
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

        async def fake_get_points(username):
            await asyncio.sleep(0)  # yield control, encouraging real interleaving
            return 10000

        mock_subtract = AsyncMock()
        monkeypatch.setattr(roulette, "get_user_points", fake_get_points)
        monkeypatch.setattr(roulette, "subtract_points", mock_subtract)
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await asyncio.gather(
            roulette.vote("a", "vandal"),
            roulette.vote("b", "vandal"),
            roulette.vote("c", "vandal"),
        )

        costs_charged = sorted(call.args[1] for call in mock_subtract.call_args_list)
        assert costs_charged == [50, 75, 100]  # still correctly escalated, no duplicates
        assert roulette._state.weights["vandal"] == 3

    @pytest.mark.asyncio
    async def test_rejects_insufficient_balance_without_incrementing_weight(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=10))
        mock_subtract = AsyncMock()
        monkeypatch.setattr(roulette, "subtract_points", mock_subtract)

        result = await roulette.vote("brokeviewer", "operator")

        assert result["ok"] is False
        mock_subtract.assert_not_called()
        assert roulette._state.weights["operator"] == 0


class TestEndRoulette:
    @pytest.mark.asyncio
    async def test_declares_the_highest_weighted_weapon_as_winner(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["phantom"] = 5
        roulette._state.weights["vandal"] = 3
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        winner = await roulette.end_roulette()

        assert winner == "phantom"
        assert roulette._state.is_active is False

    @pytest.mark.asyncio
    async def test_no_winner_declared_when_nobody_voted(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        winner = await roulette.end_roulette()

        assert winner is None

    @pytest.mark.asyncio
    async def test_ending_an_already_inactive_session_is_a_safe_no_op(self):
        winner = await roulette.end_roulette()
        assert winner is None


class TestForcedBuyBadge:
    @pytest.mark.asyncio
    async def test_ending_with_a_winner_starts_the_forced_buy_as_queued(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}
        roulette._state.weights["vandal"] = 3
        monkeypatch.setattr(config, "_data", {"forced_buy_queued_duration_seconds": 9999})  # long, so it stays "queued" for this test
        mock_broadcast = AsyncMock()
        monkeypatch.setattr(roulette.widget_hub, "broadcast", mock_broadcast)

        await roulette.end_roulette()

        assert roulette._state.forced_buy_weapon == "vandal"
        assert roulette._state.forced_buy_phase == "queued"
        broadcast_types = [call.args[0]["type"] for call in mock_broadcast.call_args_list]
        assert "forced_buy_queued" in broadcast_types

    @pytest.mark.asyncio
    async def test_no_winner_means_no_forced_buy_started(self, monkeypatch):
        roulette._state.is_active = True
        roulette._state.weights = {w: 0 for w in roulette.WEAPONS}  # nobody voted
        monkeypatch.setattr(roulette.widget_hub, "broadcast", AsyncMock())

        await roulette.end_roulette()

        assert roulette._state.forced_buy_weapon is None

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
        monkeypatch.setattr(roulette, "get_user_points", AsyncMock(return_value=1000))
        monkeypatch.setattr(roulette, "subtract_points", AsyncMock())
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
    def make_chat_event(self, username: str, message: str) -> dict:
        return {
            "event": {"type": "ChatMessage"},
            "data": {"message": {"username": username, "message": message}},
        }

    @pytest.mark.asyncio
    async def test_roulette_command_triggers_a_session(self, monkeypatch):
        mock_trigger = AsyncMock()
        monkeypatch.setattr(roulette, "trigger_roulette", mock_trigger)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!roulette"))

        mock_trigger.assert_called_once_with("someviewer")

    @pytest.mark.asyncio
    async def test_weapon_command_casts_a_vote(self, monkeypatch):
        mock_vote = AsyncMock()
        monkeypatch.setattr(roulette, "vote", mock_vote)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!vandal"))

        mock_vote.assert_called_once_with("someviewer", "vandal")

    @pytest.mark.asyncio
    async def test_ignores_messages_that_are_not_commands(self, monkeypatch):
        mock_trigger = AsyncMock()
        mock_vote = AsyncMock()
        monkeypatch.setattr(roulette, "trigger_roulette", mock_trigger)
        monkeypatch.setattr(roulette, "vote", mock_vote)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "gg that was close"))

        mock_trigger.assert_not_called()
        mock_vote.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_chat_message_events(self, monkeypatch):
        mock_trigger = AsyncMock()
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
        mock_vote = AsyncMock()
        monkeypatch.setattr(roulette, "vote", mock_vote)

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
        mock_vote = AsyncMock()
        monkeypatch.setattr(roulette, "vote", mock_vote)

        await roulette.handle_chat_command(self.make_chat_event("someviewer", "!notarealgun"))

        mock_vote.assert_called_once_with("someviewer", "notarealgun")

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
