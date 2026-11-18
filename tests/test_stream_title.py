import sys
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aioresponses import aioresponses

from core.config import config
from stream import stream_title, twitch_client


@pytest.fixture(autouse=True)
def twitch(monkeypatch):
    monkeypatch.setattr(config, "_data", {"twitch_client_id": "id", "twitch_client_secret": "s", "twitch_channel": "DualBladeX"})
    monkeypatch.setattr(twitch_client, "_cached_token", "tok")
    monkeypatch.setattr(twitch_client, "_cached_token_expiry", 9999999999)
    monkeypatch.setattr(twitch_client, "_user_ids", {})


@pytest.mark.asyncio
async def test_title_is_read_offline_and_the_user_id_cached():
    with aioresponses() as m:
        m.get("https://api.twitch.tv/helix/users?login=dualbladex", payload={"data": [{"id": "42"}]})
        m.get("https://api.twitch.tv/helix/channels?broadcaster_id=42", payload={"data": [{"title": "Radiant push | !song"}]})
        m.get("https://api.twitch.tv/helix/channels?broadcaster_id=42", payload={"data": [{"title": "Second"}]})
        assert await twitch_client.channel_title("DualBladeX") == "Radiant push | !song"
        assert await twitch_client.channel_title("dualbladex") == "Second"


@pytest.mark.asyncio
async def test_unknown_channel_is_none():
    with aioresponses() as m:
        m.get("https://api.twitch.tv/helix/users?login=dualbladex", payload={"data": []})
        assert await twitch_client.channel_title("dualbladex") is None


@pytest.mark.asyncio
async def test_tick_broadcasts_only_with_an_overlay_and_a_title(monkeypatch):
    sent = AsyncMock()
    monkeypatch.setattr(stream_title.widget_hub, "broadcast", sent)
    lookup = AsyncMock(return_value="Radiant push")
    monkeypatch.setattr(twitch_client, "channel_title", lookup)

    monkeypatch.setattr(stream_title.widget_hub, "connected_count", lambda tag=None: 0)
    await stream_title.tick()
    lookup.assert_not_awaited()

    monkeypatch.setattr(stream_title.widget_hub, "connected_count", lambda tag=None: 1)
    await stream_title.tick()
    sent.assert_awaited_once_with({"type": "stream_title", "title": "Radiant push"}, tag="title")

    lookup.return_value = None
    await stream_title.tick()
    assert sent.await_count == 1
