import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import dashboard_api
from config import config
from config_schema import SECRET_MASK


@pytest.fixture
def saved(monkeypatch):
    monkeypatch.setattr(config, "_data", {"github_client_secret": "realvalue", "http_port": 8765,
                                          "discord_live_poll_seconds": 60})
    calls = []
    monkeypatch.setattr(config, "save", lambda: calls.append(1))
    return calls


async def _client():
    app = web.Application()
    dashboard_api.register_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


@pytest.mark.asyncio
async def test_get_config_never_returns_a_raw_secret(saved):
    c = await _client()
    try:
        text = await (await c.get("/api/config")).text()
        assert "realvalue" not in text
        assert __import__("json").loads(text)["github_client_secret"] == SECRET_MASK
        r = await c.get("/api/config/schema")
        assert r.headers["Cache-Control"] == "no-store"
        assert "http_port" in (await r.json())["fields"]
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_put_with_the_mask_keeps_the_stored_secret(saved):
    c = await _client()
    try:
        r = await c.put("/api/config", json={"github_client_secret": SECRET_MASK, "http_port": 9000})
        assert r.status == 200
        assert config.get("github_client_secret") == "realvalue" and config.get("http_port") == 9000
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_put_with_one_invalid_value_writes_nothing(saved):
    c = await _client()
    try:
        r = await c.put("/api/config", json={"http_port": 9000, "discord_live_poll_seconds": 1})
        assert r.status == 400 and (await r.json())["field"] == "discord_live_poll_seconds"
        assert config.get("http_port") == 8765 and not saved
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_put_merges_custom_fields(saved):
    config._data["_custom_fields"] = {"a_key": {"type": "text", "section": "Other"}}
    c = await _client()
    try:
        r = await c.put("/api/config", json={"b_key": 3, "_custom_fields": {"b_key": {"type": "number", "section": "Editor app"}}})
        assert r.status == 200
        assert set(config.get("_custom_fields")) == {"a_key", "b_key"}
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_put_of_the_whole_object_with_unchanged_comments_saves_only_the_change(saved):
    config._data["_comment_x"] = "note"
    c = await _client()
    try:
        r = await c.put("/api/config", json={"_comment_x": "note", "http_port": 9000})
        assert r.status == 200
        assert (await r.json())["updated_keys"] == ["http_port"]
        assert saved
    finally:
        await c.close()
