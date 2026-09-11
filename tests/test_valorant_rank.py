import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import valorant_rank
from config import config

CONFIGURED = {
    "henrik_api_key": "HDEV-test",
    "riot_name": "DualBladeX",
    "riot_tag": "mao",
    "riot_region": "ap",
}


@pytest.fixture(autouse=True)
def clean_rank():
    valorant_rank.forget_cache()
    yield
    valorant_rank.forget_cache()


class TestConfiguration:
    def test_not_configured_without_a_key(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"riot_name": "x", "riot_tag": "y"})
        assert valorant_rank.is_configured() is False

    def test_not_configured_without_a_riot_id(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"henrik_api_key": "k"})
        assert valorant_rank.is_configured() is False

    @pytest.mark.asyncio
    async def test_an_unconfigured_lookup_says_what_is_missing(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        with pytest.raises(valorant_rank.RankUnavailable) as caught:
            await valorant_rank.current_rank()
        assert "henrik_api_key" in str(caught.value)


class TestDescribe:
    def test_tier_and_rr(self):
        assert valorant_rank.describe({"tier": "Diamond 2", "rr": 47}) == "Diamond 2 - 47 RR"

    def test_a_rank_with_no_rr_still_renders(self):
        """Unrated accounts report a tier and nothing else."""
        assert valorant_rank.describe({"tier": "Unranked", "rr": None}) == "Unranked"

    def test_a_missing_tier_falls_back(self):
        assert valorant_rank.describe({}) == "Unranked"


class TestCaching:
    """
    One person asking prompts three more, and HenrikDev is a free
    community API this project has no claim on. A rank cannot change in
    under a match, so nothing is lost by answering from cache.
    """

    @pytest.mark.asyncio
    async def test_a_second_ask_does_not_refetch(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        fetch = AsyncMock(return_value={"tier": "Diamond 2", "rr": 47})
        monkeypatch.setattr(valorant_rank, "_fetch", fetch)

        await valorant_rank.current_rank()
        await valorant_rank.current_rank()
        await valorant_rank.current_rank()

        assert fetch.await_count == 1

    @pytest.mark.asyncio
    async def test_a_stale_cache_is_refetched(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {**CONFIGURED, "rank_cache_seconds": 60})
        fetch = AsyncMock(return_value={"tier": "Diamond 2", "rr": 47})
        monkeypatch.setattr(valorant_rank, "_fetch", fetch)

        await valorant_rank.current_rank()
        monkeypatch.setattr(valorant_rank, "_cached_at", time.time() - 61)
        await valorant_rank.current_rank()

        assert fetch.await_count == 2

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_not_cached(self, monkeypatch):
        """Otherwise one blip silences the command for the whole cache window."""
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        monkeypatch.setattr(
            valorant_rank, "_fetch", AsyncMock(side_effect=valorant_rank.RankUnavailable("down"))
        )

        with pytest.raises(valorant_rank.RankUnavailable):
            await valorant_rank.current_rank()
        assert valorant_rank._cached is None


class TestTheChatCommand:
    def _event(self, text="!rank"):
        return {
            "event": {"source": "Twitch", "type": "ChatMessage"},
            "data": {"user": {"login": "someviewer"}, "text": text},
        }

    @pytest.mark.asyncio
    async def test_answers_with_the_rank(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        monkeypatch.setattr(
            valorant_rank, "current_rank", AsyncMock(return_value={"tier": "Diamond 2", "rr": 47})
        )
        send = AsyncMock()
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await valorant_rank.handle_chat_command(self._event())

        send.assert_awaited_once()
        assert "Diamond 2 - 47 RR" in send.await_args[0][0]

    @pytest.mark.asyncio
    async def test_rr_and_elo_are_aliases(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        monkeypatch.setattr(
            valorant_rank, "current_rank", AsyncMock(return_value={"tier": "Radiant", "rr": 412})
        )
        send = AsyncMock()
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await valorant_rank.handle_chat_command(self._event("!rr"))
        assert send.await_count == 1

    @pytest.mark.asyncio
    async def test_an_unrelated_command_is_ignored(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        send = AsyncMock()
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await valorant_rank.handle_chat_command(self._event("!roulette"))
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failure_answers_rather_than_going_quiet(self, monkeypatch):
        """
        A command that does nothing is indistinguishable from a bot that
        is down, and these failures have different fixes.
        """
        monkeypatch.setattr(config, "_data", dict(CONFIGURED))
        monkeypatch.setattr(
            valorant_rank,
            "current_rank",
            AsyncMock(side_effect=valorant_rank.RankUnavailable("the Henrik API key was refused")),
        )
        send = AsyncMock()
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await valorant_rank.handle_chat_command(self._event())

        send.assert_awaited_once()
        assert "key was refused" in send.await_args[0][0]
