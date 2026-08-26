import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses
from unittest.mock import AsyncMock

import public_api
from config import config
from server import build_app


@pytest.mark.asyncio
async def test_live_status_reads_the_twitch_specific_channel_config_key(monkeypatch):
    """
    Guards against the actual bug found: this endpoint was reading
    "streamlabs_channel" (a Streamlabs-specific key for the Loyalty Points
    REST calls) instead of a dedicated Twitch channel name, meaning it
    always sent an empty user_login to Twitch regardless of what the user
    configured. Asserts on the actual argument passed to is_channel_live,
    not just the response shape, which is what let the original bug slip
    through untested.
    """
    monkeypatch.setattr(config, "_data", {
        "twitch_channel": "dualbladex",
        "streamlabs_channel": "some_other_streamlabs_username",
    })
    mock_is_live = AsyncMock(return_value=True)
    monkeypatch.setattr(public_api, "is_channel_live", mock_is_live)

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    await client.get("/api/public/live-status")

    mock_is_live.assert_called_once_with("dualbladex")

    await client.close()


@pytest.mark.asyncio
async def test_live_status_returns_true_when_live(monkeypatch):
    monkeypatch.setattr(public_api, "is_channel_live", AsyncMock(return_value=True))

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    resp = await client.get("/api/public/live-status")
    data = await resp.json()

    assert resp.status == 200
    assert data == {"live": True}

    await client.close()


@pytest.mark.asyncio
async def test_live_status_returns_false_when_offline(monkeypatch):
    monkeypatch.setattr(public_api, "is_channel_live", AsyncMock(return_value=False))

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    resp = await client.get("/api/public/live-status")
    data = await resp.json()

    assert resp.status == 200
    assert data == {"live": False}

    await client.close()


@pytest.mark.asyncio
async def test_videos_returns_curated_list_with_metadata(monkeypatch):
    monkeypatch.setattr(public_api.config, "_data", {
        "youtube_video_ids": ["abc123", "def456"],
        "youtube_api_key": "fake-yt-key",
    })

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.get(
            "https://www.googleapis.com/youtube/v3/videos?part=snippet&id=abc123%2Cdef456&key=fake-yt-key",
            payload={
                "items": [
                    {"id": "abc123", "snippet": {"title": "5 Bombs in Ranked", "thumbnails": {"high": {"url": "https://img.youtube.com/abc123.jpg"}}}},
                    {"id": "def456", "snippet": {"title": "Insane Clutch Round", "thumbnails": {"high": {"url": "https://img.youtube.com/def456.jpg"}}}},
                ]
            },
        )
        resp = await client.get("/api/public/videos")
        data = await resp.json()

    assert resp.status == 200
    assert data == {
        "videos": [
            {"id": "abc123", "title": "5 Bombs in Ranked", "thumbnail": "https://img.youtube.com/abc123.jpg", "url": "https://www.youtube.com/watch?v=abc123"},
            {"id": "def456", "title": "Insane Clutch Round", "thumbnail": "https://img.youtube.com/def456.jpg", "url": "https://www.youtube.com/watch?v=def456"},
        ]
    }

    await client.close()


@pytest.mark.asyncio
async def test_site_config_returns_only_social_links(monkeypatch):
    monkeypatch.setattr(public_api.config, "_data", {
        "social_links": {"twitch": "https://www.twitch.tv/dualbladex"},
        "streamlabs_access_token": "should-never-appear",
    })

    app = build_app()
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()

    resp = await client.get("/api/public/site-config")
    data = await resp.json()
    body_text = await resp.text()

    assert resp.status == 200
    assert data == {"social_links": {"twitch": "https://www.twitch.tv/dualbladex"}}
    assert "should-never-appear" not in body_text

    await client.close()
