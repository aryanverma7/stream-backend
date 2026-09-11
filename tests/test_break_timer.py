import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import break_timer
from config import config


@pytest.fixture(autouse=True)
def clean_break():
    """
    Module-level state by design - there is one break happening or not
    happening, the way the real thing runs. Also stubs the broadcast, so
    no test needs a widget hub.

    Swapped by hand rather than through monkeypatch: an autouse fixture
    that takes another fixture is fine under pytest and not under the
    minimal runner this suite is also exercised with.
    """
    break_timer.reset()
    original = break_timer.widget_hub.broadcast
    break_timer.widget_hub.broadcast = AsyncMock()
    yield
    break_timer.widget_hub.broadcast = original
    break_timer.reset()


async def make_client():
    app = web.Application()
    break_timer.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


class TestStatus:
    def test_nothing_is_happening_by_default(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        status = break_timer.status()
        assert status["active"] is False
        assert status["remaining_seconds"] == 0
        assert status["overdue"] is False

    @pytest.mark.asyncio
    async def test_a_started_break_counts_down(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(10)
        status = break_timer.status()

        assert status["active"] is True
        assert status["duration_seconds"] == 600
        # Derived from the clock, so it is at most the duration and has
        # already begun ticking.
        assert 595 <= status["remaining_seconds"] <= 600

    @pytest.mark.asyncio
    async def test_remaining_is_derived_not_stored(self, monkeypatch):
        """
        Storing it would mean something to keep in step with the clock,
        and a break is the one screen where a wrong number is the only
        thing on stream.
        """
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(5)
        monkeypatch.setattr(break_timer, "_started_at", break_timer._started_at - 120)
        assert break_timer.status()["remaining_seconds"] == 180

    @pytest.mark.asyncio
    async def test_running_over_floors_at_zero_and_says_so(self, monkeypatch):
        """
        Never negative. The overlay says "back any moment" and keeps the
        screen up - hiding it would put the streamer on camera before they
        are there, and counting up would be a stopwatch of how late.
        """
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(5)
        monkeypatch.setattr(break_timer, "_started_at", break_timer._started_at - 999)

        status = break_timer.status()
        assert status["remaining_seconds"] == 0
        assert status["overdue"] is True
        assert status["active"] is True


class TestStartAndStop:
    @pytest.mark.asyncio
    async def test_starting_again_restarts_the_clock(self, monkeypatch):
        """How "give me five more minutes" works, without a separate verb."""
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(5)
        monkeypatch.setattr(break_timer, "_started_at", break_timer._started_at - 250)
        assert break_timer.status()["remaining_seconds"] < 60

        await break_timer.start(5)
        assert break_timer.status()["remaining_seconds"] > 290

    @pytest.mark.asyncio
    async def test_stopping_clears_it(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(10)
        await break_timer.stop()
        assert break_timer.status()["active"] is False

    @pytest.mark.asyncio
    async def test_the_overlay_is_told_on_every_change(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(10)
        await break_timer.stop()

        assert break_timer.widget_hub.broadcast.await_count == 2
        payload, kwargs = break_timer.widget_hub.broadcast.call_args
        assert payload[0]["type"] == "break_state"
        assert kwargs["tag"] == "break"

    @pytest.mark.asyncio
    async def test_an_absurd_duration_is_clamped(self, monkeypatch):
        """600 typed where 600 seconds was meant is ten hours of break."""
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(600)
        assert break_timer.status()["duration_seconds"] == break_timer.MAX_MINUTES * 60

    @pytest.mark.asyncio
    async def test_the_duration_is_remembered_as_the_next_default(self, monkeypatch):
        """The same streamer takes roughly the same break twice."""
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(config, "save", lambda: None)
        await break_timer.start(7)
        assert break_timer.default_minutes() == 7

    @pytest.mark.asyncio
    async def test_an_empty_message_falls_back_rather_than_showing_nothing(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        await break_timer.start(5, "   ")
        assert break_timer.status()["message"] == break_timer.DEFAULT_MESSAGE


class TestTheRoute:
    @pytest.mark.asyncio
    async def test_start_and_stop_over_http(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(config, "save", lambda: None)
        client = await make_client()

        resp = await client.post("/api/break", json={"action": "start", "minutes": 3, "message": "brb"})
        body = await resp.json()
        assert resp.status == 200
        assert body["active"] is True
        assert body["message"] == "brb"

        resp = await client.post("/api/break", json={"action": "stop"})
        assert (await resp.json())["active"] is False
        await client.close()

    @pytest.mark.asyncio
    async def test_a_get_reports_without_changing_anything(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        client = await make_client()
        resp = await client.get("/api/break")
        assert resp.status == 200
        assert (await resp.json())["active"] is False
        await client.close()

    @pytest.mark.asyncio
    async def test_an_unknown_action_is_refused(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        client = await make_client()
        resp = await client.post("/api/break", json={"action": "pause"})
        assert resp.status == 400
        await client.close()

    @pytest.mark.asyncio
    async def test_zero_minutes_is_refused_rather_than_starting_an_instant_break(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        client = await make_client()
        resp = await client.post("/api/break", json={"action": "start", "minutes": 0})
        assert resp.status == 400
        assert break_timer.status()["active"] is False
        await client.close()

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_refused_not_crashed(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        client = await make_client()
        resp = await client.post("/api/break", data=b"nope")
        assert resp.status == 400
        await client.close()
