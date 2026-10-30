import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses
from unittest.mock import AsyncMock

from core import public_api
from core.config import config
from core.server import build_app


@pytest.fixture(autouse=True)
def _no_cache():
    public_api._cache.clear()
    yield
    public_api._cache.clear()


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


def oembed(vid):
    return f"https://www.youtube.com/oembed?format=json&url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3D{vid}"


@pytest.mark.asyncio
async def test_videos_returns_curated_list_with_metadata_without_an_api_key(monkeypatch):
    monkeypatch.setattr(public_api.config, "_data", {"youtube_video_ids": ["abc123", "v=def456", "gone99"]})
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.get(oembed("abc123"), payload={"title": "5 Bombs in Ranked", "thumbnail_url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"})
        mocked.get(oembed("def456"), payload={"title": "Insane Clutch Round", "thumbnail_url": "https://i.ytimg.com/vi/def456/hqdefault.jpg"})
        mocked.get(oembed("gone99"), status=404)  # deleted or private: skipped, the rest still show
        resp = await client.get("/api/public/videos")
        data = await resp.json()
    await client.close()
    assert resp.status == 200
    assert data == {"videos": [
        {"id": "abc123", "title": "5 Bombs in Ranked", "thumbnail": "https://i.ytimg.com/vi/abc123/hqdefault.jpg",
         "url": "https://www.youtube.com/watch?v=abc123"},
        {"id": "def456", "title": "Insane Clutch Round", "thumbnail": "https://i.ytimg.com/vi/def456/hqdefault.jpg",
         "url": "https://www.youtube.com/watch?v=def456"},
    ]}


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


def test_video_id_accepts_pasted_links_and_v_prefix():
    for raw in ("eYAKjHbXiEI", "v=eYAKjHbXiEI", " https://www.youtube.com/watch?v=eYAKjHbXiEI&t=5s ",
                "https://youtu.be/eYAKjHbXiEI", "https://www.youtube.com/shorts/eYAKjHbXiEI"):
        assert public_api.video_id(raw) == "eYAKjHbXiEI", raw


@pytest.mark.asyncio
async def test_videos_all_failing_is_an_upstream_error(monkeypatch):
    monkeypatch.setattr(public_api.config, "_data", {"youtube_video_ids": ["abc"]})
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.get(oembed("abc"), status=500)
        resp = await client.get("/api/public/videos")
    await client.close()
    assert resp.status == public_api.UPSTREAM_FAILED_STATUS


@pytest.mark.asyncio
async def test_live_status_failure_never_echoes_the_exception(monkeypatch):
    leak = "https://id.twitch.tv/oauth2/token?client_secret=SUPERSECRET"
    monkeypatch.setattr(public_api, "is_channel_live", AsyncMock(side_effect=RuntimeError(leak)))
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        resp = await client.get("/api/public/live-status")
        assert "SUPERSECRET" not in await resp.text()
        assert resp.status == public_api.UPSTREAM_FAILED_STATUS
    finally:
        await client.close()


async def _get(path):
    client = TestClient(TestServer(build_app()))
    await client.start_server()
    try:
        resp = await client.get(path)
        return resp.status, await resp.json()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_live_status_is_cached_60s_on_success_and_15s_on_failure(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(public_api, "_clock", lambda: clock[0])
    monkeypatch.setattr(config, "_data", {"twitch_channel": "dualbladex"})
    live = AsyncMock(return_value=True)
    monkeypatch.setattr(public_api, "is_channel_live", live)
    assert await _get("/api/public/live-status") == (200, {"live": True})
    clock[0] += 59
    assert await _get("/api/public/live-status") == (200, {"live": True})
    assert live.await_count == 1
    clock[0] += 2
    live.side_effect = RuntimeError("down")
    assert (await _get("/api/public/live-status"))[0] == public_api.UPSTREAM_FAILED_STATUS
    clock[0] += 14
    assert (await _get("/api/public/live-status"))[0] == public_api.UPSTREAM_FAILED_STATUS
    assert live.await_count == 2
    clock[0] += 2
    live.side_effect = None
    assert await _get("/api/public/live-status") == (200, {"live": True})
    assert live.await_count == 3


@pytest.mark.asyncio
async def test_live_status_cache_is_keyed_by_the_channel(monkeypatch):
    monkeypatch.setattr(public_api, "_clock", lambda: 1000.0)
    live = AsyncMock(return_value=False)
    monkeypatch.setattr(public_api, "is_channel_live", live)
    monkeypatch.setattr(config, "_data", {"twitch_channel": "one"})
    await _get("/api/public/live-status")
    monkeypatch.setattr(config, "_data", {"twitch_channel": "two"})
    await _get("/api/public/live-status")
    assert [c.args[0] for c in live.await_args_list] == ["one", "two"]


@pytest.mark.asyncio
async def test_videos_are_cached_and_keyed_by_the_video_list(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(public_api, "_clock", lambda: clock[0])
    monkeypatch.setattr(public_api.config, "_data", {"youtube_video_ids": ["abc"]})
    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.get(oembed("abc"), payload={"title": "A", "thumbnail_url": "t"})
        first = await _get("/api/public/videos")
        assert await _get("/api/public/videos") == first  # second call answered from memory
        assert len(mocked.requests) == 1
        monkeypatch.setattr(public_api.config, "_data", {"youtube_video_ids": ["def"]})
        mocked.get(oembed("def"), payload={"title": "D", "thumbnail_url": "t"})
        assert (await _get("/api/public/videos"))[1]["videos"][0]["id"] == "def"
        clock[0] += 61
        monkeypatch.setattr(public_api.config, "_data", {"youtube_video_ids": ["abc"]})
        mocked.get(oembed("abc"), payload={"title": "A2", "thumbnail_url": "t"})
        assert (await _get("/api/public/videos"))[1]["videos"][0]["title"] == "A2"
