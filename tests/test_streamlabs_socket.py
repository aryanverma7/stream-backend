import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock
from aioresponses import aioresponses

import streamlabs_socket
from config import config


# Real donation event shape, taken directly from dev.streamlabs.com's own
# Socket API documentation example - not a simplified/imagined shape.
REAL_DONATION_EVENT = {
    "type": "donation",
    "message": [
        {
            "id": 96164121,
            "name": "test",
            "amount": "13.37",
            "formatted_amount": "$13.37",
            "message": "test donation",
            "currency": "USD",
            "to": {"name": "Sai Harsha Maddela"},
            "from": "test",
            "_id": "0820c9d5bafd768c9843f5e35c885e71",
        }
    ],
    "event_id": "evt_17e5f4dc6888767ed9799f78dfa2cabc",
}

# A platform-linked event (has "for") - must NOT be treated as a donation,
# per Streamlabs' own !eventData.for check.
REAL_FOLLOW_EVENT = {
    "type": "follow",
    "message": [{"name": "h4r5h48002"}],
    "for": "twitch_account",
}


class TestDonationToPoints:
    def test_applies_the_configured_rate(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_exchange_rate_per_inr": 100})
        assert streamlabs_socket.donation_to_points("13.37") == 1337

    def test_defaults_to_a_reasonable_rate_when_unconfigured(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        result = streamlabs_socket.donation_to_points("5.00")
        assert result == 50  # default rate of 10, matching config.example.json's placeholder

    def test_rounds_to_a_whole_number_of_points(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_exchange_rate_per_inr": 33})
        assert isinstance(streamlabs_socket.donation_to_points("1.50"), int)


class TestHandleSocketEvent:
    @pytest.mark.asyncio
    async def test_grants_points_for_a_real_donation_event(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_exchange_rate_per_inr": 100})
        mock_grant = AsyncMock(return_value=1837)
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        await streamlabs_socket.handle_socket_event(REAL_DONATION_EVENT)

        mock_grant.assert_called_once_with("test", 1337)

    @pytest.mark.asyncio
    async def test_ignores_platform_linked_events_like_follows(self, monkeypatch):
        mock_grant = AsyncMock()
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        await streamlabs_socket.handle_socket_event(REAL_FOLLOW_EVENT)

        mock_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_non_donation_types_with_no_for_key(self, monkeypatch):
        mock_grant = AsyncMock()
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        await streamlabs_socket.handle_socket_event({"type": "something_else", "message": []})

        mock_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_multiple_donations_in_one_event(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_exchange_rate_per_inr": 10})
        mock_grant = AsyncMock(return_value=100)
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        event = {
            "type": "donation",
            "message": [
                {"name": "alice", "amount": "5.00"},
                {"name": "bob", "amount": "2.50"},
            ],
        }
        await streamlabs_socket.handle_socket_event(event)

        assert mock_grant.call_count == 2
        mock_grant.assert_any_call("alice", 50)
        mock_grant.assert_any_call("bob", 25)

    @pytest.mark.asyncio
    async def test_skips_a_donation_missing_a_donor_name_without_crashing(self, monkeypatch):
        mock_grant = AsyncMock()
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        event = {"type": "donation", "message": [{"amount": "5.00"}]}  # no "name"
        await streamlabs_socket.handle_socket_event(event)

        mock_grant.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_failed_grant_does_not_crash_or_stop_other_donations_in_the_same_event(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_exchange_rate_per_inr": 10})
        mock_grant = AsyncMock(side_effect=[Exception("Streamlabs API error"), 100])
        monkeypatch.setattr(streamlabs_socket, "grant_points", mock_grant)

        event = {
            "type": "donation",
            "message": [
                {"name": "alice", "amount": "5.00"},
                {"name": "bob", "amount": "2.50"},
            ],
        }
        await streamlabs_socket.handle_socket_event(event)  # must not raise

        assert mock_grant.call_count == 2  # bob's grant still happened despite alice's failing


class TestFetchSocketToken:
    @pytest.mark.asyncio
    async def test_fetches_the_token_using_the_stored_access_token(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"streamlabs_access_token": "real-token"})

        with aioresponses() as mocked:
            mocked.get(
                "https://streamlabs.com/api/v2.0/socket/token",
                payload={"socket_token": "abc123"},
            )
            result = await streamlabs_socket.fetch_socket_token()

        assert result == "abc123"
