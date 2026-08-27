import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock

import points_cloudbot
from config import config


@pytest.fixture(autouse=True)
def clean_state():
    points_cloudbot.reset()
    yield
    points_cloudbot.reset()


class TestParseBalanceReply:
    def test_reads_the_reply_cloudbot_actually_sends(self):
        """Verbatim from this channel, not from documentation."""
        assert points_cloudbot.parse_balance_reply("@dualbladex, you have 19 Bunds.") == (
            "dualbladex",
            19,
        )

    def test_the_currency_name_is_never_matched_on(self):
        """Every streamer names their own, so matching it would break per channel."""
        assert points_cloudbot.parse_balance_reply("@viewer, you have 42 Doubloons.") == ("viewer", 42)

    def test_handles_a_thousands_separator(self):
        assert points_cloudbot.parse_balance_reply("@viewer, you have 12,500 Bunds.") == ("viewer", 12500)

    def test_usernames_come_back_lowercased(self):
        assert points_cloudbot.parse_balance_reply("@DualBladeX, you have 5 Bunds.")[0] == "dualbladex"

    def test_an_unrelated_chat_line_is_not_a_balance(self):
        assert points_cloudbot.parse_balance_reply("hey does anyone have a spare vandal") is None

    def test_a_write_confirmation_is_not_a_balance(self):
        assert points_cloudbot.parse_balance_reply("mod has successfully added 1 Bunds to viewer") is None


class TestParseWriteReply:
    def test_reads_the_add_confirmation_cloudbot_actually_sends(self):
        assert points_cloudbot.parse_write_reply(
            "dualbladex has successfully added 1 Bunds to someviewer"
        ) == ("someviewer", 1, "added")

    def test_reads_the_remove_confirmation_cloudbot_actually_sends(self):
        # Note the trailing full stop, which the add reply does not have.
        assert points_cloudbot.parse_write_reply(
            "dualbladex has successfully removed 500 Bunds from someviewer."
        ) == ("someviewer", 500, "removed")

    def test_the_target_is_read_not_the_moderator(self):
        """Both names appear in the reply and only one of them is the point."""
        assert points_cloudbot.parse_write_reply(
            "somemod has successfully removed 50 Bunds from someviewer."
        )[0] == "someviewer"

    def test_a_balance_reply_is_not_a_write(self):
        assert points_cloudbot.parse_write_reply("@viewer, you have 19 Bunds.") is None


class TestReadingABalance:
    @pytest.mark.asyncio
    async def test_asks_cloudbot_and_returns_what_it_answers(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        async def answer():
            await asyncio.sleep(0)
            await points_cloudbot.handle_chat_event({"text": "@someviewer, you have 750 Bunds."})

        reader = asyncio.ensure_future(points_cloudbot.get_user_points("someviewer"))
        await answer()

        assert await reader == 750
        assert mock_send.await_args[0][0] == "!points someviewer"

    @pytest.mark.asyncio
    async def test_a_second_read_is_served_from_cache_without_spamming_chat(self, monkeypatch):
        """
        A live read per vote would put two bot lines in chat per vote,
        which over an 18-second window is a wall of spam.
        """
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        reader = asyncio.ensure_future(points_cloudbot.get_user_points("someviewer"))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"text": "@someviewer, you have 750 Bunds."})
        await reader

        assert await points_cloudbot.get_user_points("someviewer") == 750
        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_a_reply_about_somebody_else_does_not_answer_this_lookup(self, monkeypatch):
        """Replies are matched by username, since they carry no request id."""
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        reader = asyncio.ensure_future(points_cloudbot.get_user_points("someviewer"))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"text": "@anotherviewer, you have 999 Bunds."})

        with pytest.raises(TimeoutError):
            await reader

    @pytest.mark.asyncio
    async def test_silence_raises_rather_than_guessing_a_balance(self, monkeypatch):
        """
        roulette.py turns this into "Couldn't verify your points balance
        right now" and charges nobody. Returning 0 would refuse everyone;
        returning a guess would hand out free roulettes.
        """
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.get_user_points("someviewer")

    @pytest.mark.asyncio
    async def test_a_disconnected_streamerbot_fails_immediately(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=False))

        with pytest.raises(RuntimeError):
            await points_cloudbot.get_user_points("someviewer")


class TestSpending:
    @pytest.mark.asyncio
    async def test_waits_for_cloudbot_to_confirm_the_spend(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert mock_send.await_args[0][0] == "!removepoints someviewer 500"

    @pytest.mark.asyncio
    async def test_an_unconfirmed_spend_fails_rather_than_being_assumed(self, monkeypatch):
        """
        Cloudbot owns the wallet, so this backend cannot know whether the
        viewer could afford it. Assuming success would hand out a free
        roulette; refusing costs one command.
        """
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.subtract_points("someviewer", 500)

    @pytest.mark.asyncio
    async def test_a_confirmed_spend_updates_the_held_balance(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        reader = asyncio.ensure_future(points_cloudbot.get_user_points("someviewer"))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"text": "@someviewer, you have 750 Bunds."})
        await reader

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert await points_cloudbot.get_user_points("someviewer") == 250

    @pytest.mark.asyncio
    async def test_the_held_balance_never_goes_negative(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        points_cloudbot._cache["someviewer"] = (points_cloudbot.time.monotonic(), 100)

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert points_cloudbot._cache["someviewer"][1] == 0


class TestGranting:
    @pytest.mark.asyncio
    async def test_adds_then_reads_the_new_total_back(self, monkeypatch):
        """
        Cloudbot's confirmation reports the amount added, not the total,
        and grant_points has to return a balance.
        """
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"text": "mod has successfully added 100 Bunds to someviewer"}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"text": "@someviewer, you have 850 Bunds."})

        assert await granter == 850
        sent = [call[0][0] for call in mock_send.await_args_list]
        assert sent == ["!addpoints someviewer 100", "!points someviewer"]

    @pytest.mark.asyncio
    async def test_the_read_back_ignores_the_cache(self, monkeypatch):
        """The whole point is that the number just changed."""
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        points_cloudbot._cache["someviewer"] = (points_cloudbot.time.monotonic(), 750)

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"text": "mod has successfully added 100 Bunds to someviewer"}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"text": "@someviewer, you have 850 Bunds."})

        assert await granter == 850
