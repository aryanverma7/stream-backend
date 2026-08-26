import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses
from unittest.mock import AsyncMock

import streamlabs_oauth
from config import config


async def make_client():
    app = web.Application()
    app.router.add_get("/auth/streamlabs/login", streamlabs_oauth.streamlabs_login)
    app.router.add_get("/auth/streamlabs/callback", streamlabs_oauth.streamlabs_callback)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_login_redirects_to_the_exact_verified_authorize_url(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "streamlabs_client_id": "test-client-id",
        "streamlabs_redirect_uri": "https://hub.dualbladex.org/auth/streamlabs/callback",
    })
    streamlabs_oauth._pending_states.clear()

    client = await make_client()
    resp = await client.get("/auth/streamlabs/login", allow_redirects=False)

    assert resp.status == 302
    location = resp.headers["Location"]
    assert location.startswith("https://streamlabs.com/api/v2.0/authorize?")
    assert "client_id=test-client-id" in location
    assert "response_type=code" in location
    assert "scope=socket.token" in location
    assert "state=" in location

    await client.close()


@pytest.mark.asyncio
async def test_login_fails_clearly_when_credentials_are_missing(monkeypatch):
    monkeypatch.setattr(config, "_data", {})

    client = await make_client()
    resp = await client.get("/auth/streamlabs/login", allow_redirects=False)

    assert resp.status == 400
    body = await resp.json()
    assert "config.json" in body["error"]

    await client.close()


@pytest.mark.asyncio
async def test_callback_exchanges_code_for_token_using_the_exact_verified_endpoint(monkeypatch):
    saved = {}
    monkeypatch.setattr(config, "_data", {
        "streamlabs_client_id": "test-client-id",
        "streamlabs_client_secret": "test-client-secret",
        "streamlabs_redirect_uri": "https://hub.dualbladex.org/auth/streamlabs/callback",
    })
    monkeypatch.setattr(config, "set", lambda k, v: saved.__setitem__(k, v))
    monkeypatch.setattr(config, "save", lambda: None)

    streamlabs_oauth._pending_states.clear()
    streamlabs_oauth._pending_states.add("valid-state-123")

    client = await make_client()

    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.post(
            "https://streamlabs.com/api/v2.0/token",
            payload={"access_token": "real-access-token", "refresh_token": "real-refresh-token"},
        )
        resp = await client.get(
            "/auth/streamlabs/callback?code=auth-code-xyz&state=valid-state-123",
            allow_redirects=False,
        )

    assert resp.status == 302
    assert resp.headers["Location"] == "/admin"
    assert saved["streamlabs_access_token"] == "real-access-token"
    assert saved["streamlabs_refresh_token"] == "real-refresh-token"

    await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_a_missing_or_reused_state_as_a_csrf_guard(monkeypatch):
    monkeypatch.setattr(config, "_data", {})
    streamlabs_oauth._pending_states.clear()  # deliberately empty - state was never issued

    client = await make_client()
    resp = await client.get("/auth/streamlabs/callback?code=some-code&state=never-issued", allow_redirects=False)

    assert resp.status == 400
    body = await resp.json()
    assert "state" in body["error"].lower()

    await client.close()


@pytest.mark.asyncio
async def test_callback_surfaces_streamlabs_declining_authorization(monkeypatch):
    monkeypatch.setattr(config, "_data", {})

    client = await make_client()
    resp = await client.get("/auth/streamlabs/callback?error=access_denied", allow_redirects=False)

    assert resp.status == 400
    body = await resp.json()
    assert "access_denied" in body["error"]

    await client.close()


@pytest.mark.asyncio
async def test_callback_starts_the_tips_listener_immediately_after_connecting(monkeypatch):
    """
    The actual bug this test guards against: a real user connected via the
    admin dashboard mid-session, and the backend never started listening
    for tips because it only ever checked for a token once, at startup.
    """
    monkeypatch.setattr(config, "_data", {
        "streamlabs_client_id": "test-client-id",
        "streamlabs_client_secret": "test-client-secret",
        "streamlabs_redirect_uri": "https://hub.dualbladex.org/auth/streamlabs/callback",
    })
    monkeypatch.setattr(config, "set", lambda k, v: None)
    monkeypatch.setattr(config, "save", lambda: None)

    mock_stop = AsyncMock()
    mock_start = AsyncMock()
    monkeypatch.setattr(streamlabs_oauth, "stop_tips_listener", mock_stop)
    monkeypatch.setattr(streamlabs_oauth, "start_tips_listener", mock_start)

    streamlabs_oauth._pending_states.clear()
    streamlabs_oauth._pending_states.add("valid-state-789")

    client = await make_client()

    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.post(
            "https://streamlabs.com/api/v2.0/token",
            payload={"access_token": "real-access-token"},
        )
        await client.get(
            "/auth/streamlabs/callback?code=auth-code&state=valid-state-789",
            allow_redirects=False,
        )

    mock_stop.assert_called_once()
    mock_start.assert_called_once()

    await client.close()


@pytest.mark.asyncio
async def test_callback_still_succeeds_even_if_the_tips_listener_fails_to_start(monkeypatch):
    """
    A Streamlabs Socket API hiccup shouldn't break the OAuth connection
    itself - the token is already saved and valid regardless of whether
    the real-time listener manages to connect right away.
    """
    monkeypatch.setattr(config, "_data", {
        "streamlabs_client_id": "test-client-id",
        "streamlabs_client_secret": "test-client-secret",
        "streamlabs_redirect_uri": "https://hub.dualbladex.org/auth/streamlabs/callback",
    })
    monkeypatch.setattr(config, "set", lambda k, v: None)
    monkeypatch.setattr(config, "save", lambda: None)
    monkeypatch.setattr(streamlabs_oauth, "stop_tips_listener", AsyncMock())
    monkeypatch.setattr(streamlabs_oauth, "start_tips_listener", AsyncMock(side_effect=Exception("socket connection failed")))

    streamlabs_oauth._pending_states.clear()
    streamlabs_oauth._pending_states.add("valid-state-999")

    client = await make_client()

    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.post(
            "https://streamlabs.com/api/v2.0/token",
            payload={"access_token": "real-access-token"},
        )
        resp = await client.get(
            "/auth/streamlabs/callback?code=auth-code&state=valid-state-999",
            allow_redirects=False,
        )

    assert resp.status == 302
    assert resp.headers["Location"] == "/admin"

    await client.close()
    monkeypatch.setattr(config, "_data", {
        "streamlabs_client_id": "test-client-id",
        "streamlabs_client_secret": "wrong-secret",
        "streamlabs_redirect_uri": "https://hub.dualbladex.org/auth/streamlabs/callback",
    })
    streamlabs_oauth._pending_states.clear()
    streamlabs_oauth._pending_states.add("valid-state-456")

    client = await make_client()

    with aioresponses(passthrough=["http://127.0.0.1"]) as mocked:
        mocked.post(
            "https://streamlabs.com/api/v2.0/token",
            status=400,
            payload={"error": "invalid_client"},
        )
        resp = await client.get(
            "/auth/streamlabs/callback?code=auth-code&state=valid-state-456",
            allow_redirects=False,
        )

    assert resp.status == 502
    body = await resp.json()
    assert body["error"] == "invalid_client"

    await client.close()
