import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aioresponses import aioresponses

from stream import twitch_client
from core.config import config


@pytest.mark.asyncio
async def test_get_app_token_fetches_and_caches(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "twitch_client_id": "test-client-id",
        "twitch_client_secret": "test-client-secret",
    })
    twitch_client._cached_token = None
    twitch_client._cached_token_expiry = 0

    with aioresponses() as mocked:
        mocked.post(
            "https://id.twitch.tv/oauth2/token",
            payload={"access_token": "fake-app-token", "expires_in": 5184000, "token_type": "bearer"},
        )
        token = await twitch_client.get_app_token()
        (url, call), = [(k[1], v[0]) for k, v in mocked.requests.items()]
        # The secret travels in the form body, never the URL (which reaches logs and error text).
        assert "test-client-secret" not in str(url)
        assert call.kwargs["data"]["client_secret"] == "test-client-secret"

    assert token == "fake-app-token"

    token_again = await twitch_client.get_app_token()
    assert token_again == "fake-app-token"


@pytest.mark.asyncio
async def test_is_channel_live_returns_true_when_stream_found(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "twitch_client_id": "test-client-id",
        "twitch_client_secret": "test-client-secret",
    })
    twitch_client._cached_token = "fake-app-token"
    twitch_client._cached_token_expiry = 9999999999

    with aioresponses() as mocked:
        mocked.get(
            "https://api.twitch.tv/helix/streams?user_login=dualbladex",
            payload={"data": [{"id": "12345", "user_login": "dualbladex", "type": "live"}]},
        )
        result = await twitch_client.is_channel_live("dualbladex")

    assert result is True


@pytest.mark.asyncio
async def test_is_channel_live_returns_false_when_no_stream(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "twitch_client_id": "test-client-id",
        "twitch_client_secret": "test-client-secret",
    })
    twitch_client._cached_token = "fake-app-token"
    twitch_client._cached_token_expiry = 9999999999

    with aioresponses() as mocked:
        mocked.get(
            "https://api.twitch.tv/helix/streams?user_login=dualbladex",
            payload={"data": []},
        )
        result = await twitch_client.is_channel_live("dualbladex")

    assert result is False


@pytest.mark.asyncio
async def test_token_error_carries_no_secret(monkeypatch):
    monkeypatch.setattr(config, "_data", {"twitch_client_id": "id", "twitch_client_secret": "SUPERSECRET"})
    twitch_client._cached_token = None
    with aioresponses() as mocked:
        mocked.post("https://id.twitch.tv/oauth2/token", status=400)
        with pytest.raises(twitch_client.TwitchError) as e:
            await twitch_client.get_app_token()
    assert "SUPERSECRET" not in str(e.value) and e.value.status == 400


@pytest.mark.asyncio
async def test_401_drops_the_cached_token(monkeypatch):
    monkeypatch.setattr(config, "_data", {"twitch_client_id": "id", "twitch_client_secret": "s"})
    twitch_client._cached_token = "stale"
    twitch_client._cached_token_expiry = 9999999999
    with aioresponses() as mocked:
        mocked.get("https://api.twitch.tv/helix/streams?user_login=x", status=401)
        with pytest.raises(twitch_client.TwitchError):
            await twitch_client.stream_info("x")
    assert twitch_client._cached_token is None
