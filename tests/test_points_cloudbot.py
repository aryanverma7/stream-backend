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


def _cache_now(username, balance, platform="twitch"):
    """Seeds the cache the way real chat does - a viewer typing !points."""
    key = points_cloudbot._key(platform, username)
    points_cloudbot._cache[key] = (points_cloudbot.time.monotonic(), balance)


class TestReadingABalance:
    """
    get_user_points serves the cache or raises. It never asks, because
    `!points <name>` ignores its argument: dualbladex typing
    `!points pinkuthagoat` got "@dualbladex, you have 961 Bunds." back
    while pinkuthagoat held 1760.
    """

    @pytest.mark.asyncio
    async def test_never_sends_a_command(self, monkeypatch):
        """The command it would send returns the bot's own balance."""
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        with pytest.raises(points_cloudbot.CloudbotReadUnavailable):
            await points_cloudbot.get_user_points("someviewer")

        assert mock_send.await_count == 0

    @pytest.mark.asyncio
    async def test_serves_a_balance_the_viewer_revealed_themselves(self):
        """
        A viewer typing !points about themselves goes past
        handle_chat_event, which is how the cache fills up without this
        module ever asking.
        """
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "@someviewer, you have 750 Bunds."})
        assert await points_cloudbot.get_user_points("someviewer") == 750

    @pytest.mark.asyncio
    async def test_a_stale_entry_is_not_served(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_cache_ttl_seconds": 0})
        _cache_now("someviewer", 750)

        with pytest.raises(points_cloudbot.CloudbotReadUnavailable):
            await points_cloudbot.get_user_points("someviewer")

    @pytest.mark.asyncio
    async def test_a_reply_about_somebody_else_does_not_fill_this_entry(self):
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "@anotherviewer, you have 999 Bunds."})

        with pytest.raises(points_cloudbot.CloudbotReadUnavailable):
            await points_cloudbot.get_user_points("someviewer")

    @pytest.mark.asyncio
    async def test_the_fallback_reply_is_filed_under_the_caller_not_the_target(self):
        """
        The dangerous shape. Cloudbot answered `!points pinkuthagoat` with
        "@dualbladex, you have 961 Bunds." - matching on the name Cloudbot
        prints is what stops 961 being recorded as pinkuthagoat's balance.
        """
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "@dualbladex, you have 961 Bunds."})

        assert await points_cloudbot.get_user_points("dualbladex") == 961
        with pytest.raises(points_cloudbot.CloudbotReadUnavailable):
            await points_cloudbot.get_user_points("pinkuthagoat")


class TestTrySpend:
    """
    Affordability without a read, which is the only way this backend can
    do it. Cloudbot clamps: `!removepoints pinkuthagoat 99999` against a
    balance of 1760 answered "successfully removed 1760 Bunds from
    pinkuthagoat." So the confirmation's number is what was really taken.
    """

    @pytest.mark.asyncio
    async def test_a_full_spend_reports_paid(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 500 Bunds from someviewer."}
        )

        assert await spender == (True, None)
        assert mock_send.await_args[0][0] == "!removepoints someviewer 500"

    @pytest.mark.asyncio
    async def test_a_clamped_spend_reports_not_paid_and_the_real_balance(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 120 Bunds from someviewer."}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 120 Bunds to someviewer"}
        )

        assert await spender == (False, 120)

    @pytest.mark.asyncio
    async def test_a_clamped_spend_gives_back_exactly_what_it_took(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 120 Bunds from someviewer."}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 120 Bunds to someviewer"}
        )
        await spender

        sent = [call[0][0] for call in mock_send.await_args_list]
        assert sent == ["!removepoints someviewer 500", "!addpoints someviewer 120"]

    @pytest.mark.asyncio
    async def test_a_broke_viewer_is_not_refunded_zero(self, monkeypatch):
        """Nothing was taken, so there is nothing to give back - and an
        `!addpoints someviewer 0` would be a pointless line in chat."""
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 0 Bunds from someviewer."}
        )

        assert await spender == (False, 0)
        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_a_clamped_spend_records_the_balance_it_learned(self, monkeypatch):
        """The one moment this backend ever learns a viewer's exact balance."""
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 120 Bunds from someviewer."}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 120 Bunds to someviewer"}
        )
        await spender

        assert await points_cloudbot.get_user_points("someviewer") == 120

    @pytest.mark.asyncio
    async def test_a_paid_spend_updates_the_held_balance(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        _cache_now("someviewer", 750)

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert await points_cloudbot.get_user_points("someviewer") == 250

    @pytest.mark.asyncio
    async def test_an_unconfirmed_spend_raises_rather_than_reporting_paid(self, monkeypatch):
        """Silence must never become (True, None) - that is a free roulette."""
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.try_spend("someviewer", 500)

    @pytest.mark.asyncio
    async def test_a_failed_refund_raises_rather_than_reporting_not_paid(self, monkeypatch):
        """
        The viewer is genuinely down the points at that moment. Reporting
        a plain "you can't afford this" would hide it; raising surfaces it
        as an error, and the log line carries the command to fix it.
        """
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 120 Bunds from someviewer."}
        )

        with pytest.raises(TimeoutError):
            await spender

    @pytest.mark.asyncio
    async def test_an_unknown_user_raises(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(points_cloudbot.try_spend("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "Unable to find someviewer."})

        with pytest.raises(points_cloudbot.CloudbotUserNotFound):
            await spender

    @pytest.mark.asyncio
    async def test_a_disconnected_streamerbot_fails_immediately(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=False))

        with pytest.raises(RuntimeError):
            await points_cloudbot.try_spend("someviewer", 500)


class TestSpending:
    """subtract_points - unconditional, and used where affordability is
    not the question. Cloudbot clamps here too, so it quietly takes what
    is there; callers that care use try_spend."""

    @pytest.mark.asyncio
    async def test_waits_for_cloudbot_to_confirm_the_spend(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert mock_send.await_args[0][0] == "!removepoints someviewer 500"

    @pytest.mark.asyncio
    async def test_an_unconfirmed_spend_fails_rather_than_being_assumed(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.subtract_points("someviewer", 500)

    @pytest.mark.asyncio
    async def test_a_confirmed_spend_updates_the_held_balance(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        _cache_now("someviewer", 750)

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert await points_cloudbot.get_user_points("someviewer") == 250

    @pytest.mark.asyncio
    async def test_the_held_balance_never_goes_negative(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        _cache_now("someviewer", 100)

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 500))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 500 Bunds from someviewer."}
        )
        await spender

        assert points_cloudbot._cache[points_cloudbot._key("twitch", "someviewer")][1] == 0


class TestGranting:
    @pytest.mark.asyncio
    async def test_adds_to_a_held_balance_and_returns_the_new_total(self, monkeypatch):
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)
        _cache_now("someviewer", 750)

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 100 Bunds to someviewer"}
        )

        assert await granter == 850
        assert mock_send.await_args_list[0][0][0] == "!addpoints someviewer 100"

    @pytest.mark.asyncio
    async def test_sends_exactly_one_command(self, monkeypatch):
        """There is no total to read back, so nothing tries to."""
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 100 Bunds to someviewer"}
        )
        await granter

        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_returns_none_when_no_balance_was_held(self, monkeypatch):
        """
        Cloudbot's confirmation reports the amount added, not a total, and
        no total can be read back. None is the honest answer; a zero would
        be read as "this viewer has nothing".
        """
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 100 Bunds to someviewer"}
        )

        assert await granter is None

    @pytest.mark.asyncio
    async def test_an_unconfirmed_grant_raises(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.grant_points("someviewer", 100)


class TestUnknownUser:
    """
    Cloudbot answers a write about a user it has never seen with
    "Unable to find <name>." - captured verbatim from this channel. It is
    an answer, not a silence, so nothing here should sit out the timeout.
    """

    def test_parses_the_not_found_reply(self):
        assert points_cloudbot.parse_not_found_reply("Unable to find pinkudagoat.") == "pinkudagoat"

    def test_parses_it_with_an_at_sign(self):
        assert points_cloudbot.parse_not_found_reply("Unable to find @SomeViewer.") == "someviewer"

    def test_ignores_an_empty_name(self):
        """`!addpoints 10` (no user) came back "Unable to find ." - nothing to resolve."""
        assert points_cloudbot.parse_not_found_reply("Unable to find .") is None

    def test_is_not_confused_with_a_balance_reply(self):
        assert points_cloudbot.parse_not_found_reply("@someviewer, you have 19 Bunds.") is None

    @pytest.mark.asyncio
    async def test_a_spend_on_an_unknown_user_raises_rather_than_timing_out(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(points_cloudbot.subtract_points("someviewer", 50))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "Unable to find someviewer."})

        with pytest.raises(points_cloudbot.CloudbotUserNotFound):
            await spender

    @pytest.mark.asyncio
    async def test_a_grant_on_an_unknown_user_raises(self, monkeypatch):
        """Cloudbot cannot create a wallet, so this must not report success."""
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 50))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event({"platform": "twitch", "text": "Unable to find someviewer."})

        with pytest.raises(points_cloudbot.CloudbotUserNotFound):
            await granter


class TestPerPlatformWallets:
    """
    Cloudbot keeps a separate wallet per platform and only resolves a
    username in the chat the command was typed in. So the command has to
    go to the VIEWER's chat, and a reply arriving in one chat must never
    answer a lookup made in the other.

    This is the bug that made YouTube look impossible: every YouTube spend
    was being sent to Twitch chat, where that handle does not exist, and
    the resulting silence was read as Cloudbot refusing to serve YouTube.
    """

    @pytest.mark.asyncio
    async def test_the_command_goes_to_the_viewers_own_chat(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_platforms": []})
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        spender = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 350, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "mod has successfully removed 350 Bunds from someviewer."}
        )

        assert await spender == (True, None)
        assert mock_send.await_args.kwargs["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_a_reply_in_the_other_chat_does_not_answer_this_spend(self, monkeypatch):
        """The same handle on two platforms is two different wallets."""
        monkeypatch.setattr(
            config,
            "_data",
            {"cloudbot_reply_timeout_seconds": 0.05, "cloudbot_silent_write_platforms": []},
        )
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 350, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 350 Bunds from someviewer."}
        )

        with pytest.raises(TimeoutError):
            await spender

    @pytest.mark.asyncio
    async def test_balances_are_held_per_platform(self):
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "@someviewer, you have 750 Bunds."}
        )
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "@someviewer, you have 120 Bunds."}
        )

        assert await points_cloudbot.get_user_points("someviewer", "twitch") == 750
        assert await points_cloudbot.get_user_points("someviewer", "youtube") == 120

    @pytest.mark.asyncio
    async def test_an_unknown_user_on_one_platform_is_not_unknown_on_the_other(self, monkeypatch):
        monkeypatch.setattr(
            config,
            "_data",
            {"cloudbot_reply_timeout_seconds": 0.05, "cloudbot_silent_write_platforms": []},
        )
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 350, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "Unable to find someviewer."}
        )

        with pytest.raises(TimeoutError):
            await spender

    @pytest.mark.asyncio
    async def test_two_platforms_can_be_charged_at_once(self, monkeypatch):
        """Locks are per (platform, user), so one does not queue behind the other."""
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_platforms": []})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        twitch = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 50, platform="twitch")
        )
        youtube = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 50, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "mod has successfully removed 50 Bunds from someviewer."}
        )
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully removed 50 Bunds from someviewer."}
        )

        assert await youtube == (True, None)
        assert await twitch == (True, None)

    @pytest.mark.asyncio
    async def test_no_platform_falls_back_to_the_configured_one(self, monkeypatch):
        """Donations and the dashboard's manual tools reach that state."""
        monkeypatch.setattr(config, "_data", {"cloudbot_platform": "twitch"})
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        granter = asyncio.ensure_future(points_cloudbot.grant_points("someviewer", 100))
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "twitch", "text": "mod has successfully added 100 Bunds to someviewer"}
        )
        await granter

        assert mock_send.await_args.kwargs["platform"] == "twitch"


class TestSilentWritePlatforms:
    """
    On YouTube, Cloudbot's mod commands take effect and say nothing:
    `!addpoints <name> 1000` moved a balance from 560 to 1560 with no
    reply, while the same command on Twitch answers "<mod> has
    successfully added ...". Failures still reply there ("Unable to find
    <name>."), which is the only signal that makes this workable -
    silence means it went through.
    """

    @pytest.mark.asyncio
    async def test_silence_on_youtube_counts_as_a_successful_spend(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 0.05})
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)

        assert await points_cloudbot.try_spend("someviewer", 350, platform="youtube") == (True, None)
        assert mock_send.await_args[0][0] == "!removepoints someviewer 350"
        assert mock_send.await_args.kwargs["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_silence_on_twitch_is_still_a_failure(self, monkeypatch):
        """Twitch does confirm, so nothing arriving there means something is wrong."""
        monkeypatch.setattr(config, "_data", {"cloudbot_reply_timeout_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        with pytest.raises(TimeoutError):
            await points_cloudbot.try_spend("someviewer", 350, platform="twitch")

    @pytest.mark.asyncio
    async def test_a_rejection_inside_the_grace_window_still_raises(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 1})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 350, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "Unable to find someviewer."}
        )

        with pytest.raises(points_cloudbot.CloudbotUserNotFound):
            await spender

    @pytest.mark.asyncio
    async def test_a_confirmation_that_does_arrive_is_used(self, monkeypatch):
        """
        So a platform that starts answering is handled correctly without
        a config change - including its clamp.
        """
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 1})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))

        spender = asyncio.ensure_future(
            points_cloudbot.try_spend("someviewer", 350, platform="youtube")
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "mod has successfully removed 120 Bunds from someviewer."}
        )
        await asyncio.sleep(0)
        await points_cloudbot.handle_chat_event(
            {"platform": "youtube", "text": "mod has successfully added 120 Bunds to someviewer"}
        )

        assert await spender == (False, 120)

    @pytest.mark.asyncio
    async def test_a_held_balance_refuses_a_broke_viewer_without_spending(self, monkeypatch):
        """
        A clamp is invisible here, so a balance we already hold is the
        only chance to catch someone who plainly cannot pay - and it
        costs no chat line.
        """
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 0.05})
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)
        _cache_now("someviewer", 100, platform="youtube")

        assert await points_cloudbot.try_spend("someviewer", 350, platform="youtube") == (False, 100)
        mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_held_balance_that_covers_it_still_spends(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        _cache_now("someviewer", 1000, platform="youtube")

        assert await points_cloudbot.try_spend("someviewer", 350, platform="youtube") == (True, None)
        assert await points_cloudbot.get_user_points("someviewer", "youtube") == 650

    @pytest.mark.asyncio
    async def test_a_stale_balance_is_not_used_to_refuse(self, monkeypatch):
        """Cloudbot keeps accruing while we hold it - an old number is not a reason to refuse."""
        monkeypatch.setattr(
            config,
            "_data",
            {"cloudbot_silent_write_grace_seconds": 0.05, "cloudbot_cache_ttl_seconds": 0},
        )
        mock_send = AsyncMock(return_value=True)
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", mock_send)
        _cache_now("someviewer", 100, platform="youtube")

        assert await points_cloudbot.try_spend("someviewer", 350, platform="youtube") == (True, None)
        assert mock_send.await_count == 1

    @pytest.mark.asyncio
    async def test_a_grant_on_a_silent_platform_confirms_without_a_reply(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_grace_seconds": 0.05})
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=True))
        _cache_now("someviewer", 500, platform="youtube")

        assert await points_cloudbot.grant_points("someviewer", 100, platform="youtube") == 600

    @pytest.mark.asyncio
    async def test_a_disconnected_streamerbot_still_fails_immediately(self, monkeypatch):
        monkeypatch.setattr(points_cloudbot.streamerbot, "send_chat_message", AsyncMock(return_value=False))

        with pytest.raises(RuntimeError):
            await points_cloudbot.try_spend("someviewer", 350, platform="youtube")

    @pytest.mark.asyncio
    async def test_the_silent_platform_list_is_configurable(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"cloudbot_silent_write_platforms": ["twitch"]})

        assert points_cloudbot.writes_are_confirmed("youtube") is True
        assert points_cloudbot.writes_are_confirmed("Twitch") is False
