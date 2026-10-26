import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import aiohttp
import pytest
from aioresponses import aioresponses

import db
import discord_dm
from config import config

API = discord_dm.API
CH = f"{API}/users/@me/channels"


@pytest.fixture(autouse=True)
def setup(monkeypatch, tmp_path):
    db.use_path_for_tests(str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "_data", {"discord_bot_token": "tok"})


def posts(m, url):
    return [v for k, vs in m.requests.items() if k[0] == "POST" and str(k[1]) == url for v in vs]


@pytest.mark.asyncio
async def test_send_creates_channel_once_and_caches_it():
    with aioresponses() as m:
        m.post(CH, payload={"id": "c1"})
        m.post(f"{API}/channels/c1/messages", payload={"id": "m1"}, repeat=True)
        assert await discord_dm.send_dm("u1", "hi") == "m1"
        assert await discord_dm.send_dm("u1", "again") == "m1"
        assert len(posts(m, CH)) == 1
        assert posts(m, CH)[0].kwargs["json"] == {"recipient_id": "u1"}
        assert posts(m, CH)[0].kwargs["headers"]["Authorization"] == "Bot tok"
    assert db.meta_get("discord_dm_channel_u1") == "c1"


@pytest.mark.asyncio
async def test_cached_channel_404_recreates_once():
    db.meta_set("discord_dm_channel_u1", "old")
    with aioresponses() as m:
        m.post(f"{API}/channels/old/messages", status=404, payload={"code": 10003})
        m.post(CH, payload={"id": "new"})
        m.post(f"{API}/channels/new/messages", payload={"id": "m2"})
        assert await discord_dm.send_dm("u1", "hi") == "m2"
    assert db.meta_get("discord_dm_channel_u1") == "new"


@pytest.mark.asyncio
async def test_429_waits_and_retries_once(monkeypatch):
    slept = []

    async def fake(s):
        slept.append(s)

    monkeypatch.setattr(discord_dm.asyncio, "sleep", fake)
    with aioresponses() as m:
        m.post(CH, payload={"id": "c1"})
        m.post(f"{API}/channels/c1/messages", status=429, payload={"retry_after": 1.5})
        m.post(f"{API}/channels/c1/messages", payload={"id": "m3"})
        assert await discord_dm.send_dm("u1", "hi") == "m3"
    assert slept == [1.5]


@pytest.mark.asyncio
async def test_429_too_long_raises():
    with aioresponses() as m:
        m.post(CH, status=429, payload={"retry_after": 60.0})
        with pytest.raises(discord_dm.DiscordError) as e:
            await discord_dm.send_dm("u1", "hi")
    assert e.value.status == 429 and e.value.retry_after == 60.0


@pytest.mark.asyncio
async def test_403_raises_with_status():
    with aioresponses() as m:
        m.post(CH, status=403, payload={"code": 50007, "message": "Cannot send"})
        with pytest.raises(discord_dm.DiscordError) as e:
            await discord_dm.send_dm("u1", "hi")
    assert e.value.status == 403


@pytest.mark.asyncio
async def test_empty_token_raises_not_configured(monkeypatch):
    monkeypatch.setattr(config, "_data", {})
    with pytest.raises(discord_dm.DiscordNotConfigured):
        await discord_dm.send_dm("u1", "hi")


@pytest.mark.asyncio
async def test_long_content_truncated_to_2000():
    with aioresponses() as m:
        m.post(CH, payload={"id": "c1"})
        m.post(f"{API}/channels/c1/messages", payload={"id": "m"})
        await discord_dm.send_dm("u1", "x" * 3000)
        sent = posts(m, f"{API}/channels/c1/messages")[0].kwargs["json"]["content"]
    assert len(sent) == 2000 and sent.endswith("…")


@pytest.mark.asyncio
async def test_network_error_is_status_0():
    with aioresponses() as m:
        m.post(CH, exception=aiohttp.ClientConnectionError("boom"))
        with pytest.raises(discord_dm.DiscordError) as e:
            await discord_dm.send_dm("u1", "hi")
    assert e.value.status == 0
