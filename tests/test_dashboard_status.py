import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core import dashboard_api
from stream import spotify


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(dashboard_api, "_spotify_check", {"task": None, "at": None}, raising=False)


@pytest.mark.asyncio
async def test_status_does_not_wait_on_spotify_and_checks_it_at_most_once_a_minute(monkeypatch):
    calls, release = [], asyncio.Event()

    async def slow_account():
        calls.append(1)
        await release.wait()

    clock = [1000.0]
    monkeypatch.setattr(spotify, "ensure_account", slow_account)
    real = getattr(dashboard_api, "_check_spotify_account", None)
    monkeypatch.setattr(dashboard_api, "_check_spotify_account", lambda: real(clock[0]), raising=False)
    app = web.Application()
    dashboard_api.register_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        r = await asyncio.wait_for(c.get("/api/status"), 2)  # Spotify never answers; status still does
        assert r.status == 200 and "spotify" in await r.json()
        release.set()
        await asyncio.sleep(0)
        clock[0] += 59
        await c.get("/api/status")
        assert len(calls) == 1
        clock[0] += 2
        await c.get("/api/status")
        await asyncio.sleep(0)
        assert len(calls) == 2
    finally:
        release.set()
        await c.close()
