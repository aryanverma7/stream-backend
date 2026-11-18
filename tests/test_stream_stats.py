import sys
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses

from core.config import config
from stream import discord_live, stream_stats, twitch_client, twitch_oauth, youtube_client


@pytest.fixture(autouse=True)
def cfg(monkeypatch):
    data = {"twitch_client_id": "cid", "twitch_client_secret": "csecret", "twitch_channel": "DualBladeX",
            "twitch_refresh_token": "r1", "youtube_api_key": "YTKEY", "youtube_channel_id": "UC1",
            "public_base_url": "https://hub.example", "follower_goal_title": "Road to 1K", "follower_goal_target": 1000}
    monkeypatch.setattr(config, "_data", data)
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(twitch_client, "_cached_token", "app")
    monkeypatch.setattr(twitch_client, "_cached_token_expiry", 9999999999)
    monkeypatch.setattr(twitch_client, "_user_token", None)
    monkeypatch.setattr(twitch_client, "_user_ids", {"dualbladex": "42"})
    monkeypatch.setattr(youtube_client, "_video_id", (0.0, ""))
    return data


# ---------- Twitch follower total ----------

@pytest.mark.asyncio
async def test_follower_total_refreshes_the_user_token_and_saves_a_rotated_one(cfg):
    with aioresponses() as m:
        m.post("https://id.twitch.tv/oauth2/token", payload={"access_token": "user", "expires_in": 14000, "refresh_token": "r2"})
        m.get("https://api.twitch.tv/helix/channels/followers?broadcaster_id=42&first=1", payload={"total": 987, "data": []})
        assert await twitch_client.follower_total("dualbladex") == 987
        (_, call), = [(k, v[0]) for k, v in m.requests.items() if k[0] == "GET"]
        assert call.kwargs["headers"]["Authorization"] == "Bearer user"
    assert cfg["twitch_refresh_token"] == "r2"


@pytest.mark.asyncio
async def test_follower_total_is_none_until_twitch_is_connected(cfg):
    cfg["twitch_refresh_token"] = ""
    assert await twitch_client.follower_total("dualbladex") is None


@pytest.mark.asyncio
async def test_a_401_drops_the_user_token(cfg):
    twitch_client._user_token, twitch_client._user_token_expiry = "stale", 9999999999
    with aioresponses() as m:
        m.get("https://api.twitch.tv/helix/channels/followers?broadcaster_id=42&first=1", status=401)
        with pytest.raises(twitch_client.TwitchError):
            await twitch_client.follower_total("dualbladex")
    assert twitch_client._user_token is None


# ---------- Connect Twitch ----------

async def oauth_client():
    app = web.Application()
    app.router.add_get("/auth/twitch/login", twitch_oauth.twitch_login)
    app.router.add_get("/auth/twitch/callback", twitch_oauth.twitch_callback)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_connect_twitch_round_trip_saves_the_refresh_token(cfg):
    twitch_oauth._pending_states.clear()
    client = await oauth_client()
    resp = await client.get("/auth/twitch/login", allow_redirects=False)
    loc = resp.headers["Location"]
    assert loc.startswith("https://id.twitch.tv/oauth2/authorize?")
    q = parse_qs(urlsplit(loc).query)
    assert q["redirect_uri"] == ["https://hub.example/auth/twitch/callback"]
    assert q["scope"] == ["moderator:read:followers"]
    state = q["state"][0]

    with aioresponses(passthrough=["http://127.0.0.1"]) as m:
        m.post("https://id.twitch.tv/oauth2/token", payload={"access_token": "a", "refresh_token": "fresh"})
        resp = await client.get(f"/auth/twitch/callback?code=c&state={state}", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/admin"
    assert cfg["twitch_refresh_token"] == "fresh"

    resp = await client.get(f"/auth/twitch/callback?code=c&state={state}", allow_redirects=False)
    assert resp.status == 400  # a state is good once
    await client.close()


# ---------- YouTube ----------

@pytest.mark.asyncio
async def test_subscribers_send_the_key_in_a_header_not_the_url():
    with aioresponses() as m:
        m.get("https://www.googleapis.com/youtube/v3/channels?part=statistics&id=UC1",
              payload={"items": [{"statistics": {"subscriberCount": "1230"}}]})
        assert await youtube_client.subscriber_count() == 1230
        (key, calls), = m.requests.items()
        assert "YTKEY" not in str(key[1])
        assert calls[0].kwargs["headers"]["X-goog-api-key"] == "YTKEY"


@pytest.mark.asyncio
async def test_live_viewers_zero_when_not_live_and_the_page_is_not_refetched(monkeypatch):
    lookup = AsyncMock(return_value="")
    monkeypatch.setattr(discord_live, "youtube_live_video_id", lookup)
    assert await youtube_client.live_viewers(now=1000) == 0
    assert await youtube_client.live_viewers(now=1030) == 0
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_live_viewers_read_concurrent_viewers(monkeypatch):
    monkeypatch.setattr(discord_live, "youtube_live_video_id", AsyncMock(return_value="abcdefghijk"))
    with aioresponses() as m:
        m.get("https://www.googleapis.com/youtube/v3/videos?part=liveStreamingDetails&id=abcdefghijk",
              payload={"items": [{"liveStreamingDetails": {"concurrentViewers": "57"}}]})
        assert await youtube_client.live_viewers(now=1000) == 57


@pytest.mark.asyncio
async def test_no_key_means_unknown_not_zero(cfg):
    cfg["youtube_api_key"] = ""
    assert await youtube_client.live_viewers() is None
    assert await youtube_client.subscriber_count() is None


# ---------- the broadcast ----------

@pytest.mark.asyncio
async def test_snapshot_marks_failures_null_and_offline_zero(monkeypatch):
    monkeypatch.setattr(twitch_client, "stream_info", AsyncMock(return_value=None))
    monkeypatch.setattr(twitch_client, "follower_total", AsyncMock(side_effect=twitch_client.TwitchError("x", 500)))
    monkeypatch.setattr(youtube_client, "live_viewers", AsyncMock(return_value=12))
    monkeypatch.setattr(youtube_client, "subscriber_count", AsyncMock(return_value=340))
    snap = await stream_stats.snapshot()
    assert snap == {"type": "stream_stats",
                    "twitch": {"viewers": 0, "followers": None},
                    "youtube": {"viewers": 12, "subscribers": 340},
                    "goal": {"title": "Road to 1K", "target": 1000}}


@pytest.mark.asyncio
async def test_tick_asks_nothing_without_an_overlay(monkeypatch):
    snap = AsyncMock()
    monkeypatch.setattr(stream_stats, "snapshot", snap)
    monkeypatch.setattr(stream_stats.widget_hub, "connected_count", lambda tag=None: 0)
    await stream_stats.tick()
    snap.assert_not_awaited()


def test_a_bad_goal_target_is_zero(cfg):
    cfg["follower_goal_target"] = "lots"
    assert stream_stats.goal()["target"] == 0
