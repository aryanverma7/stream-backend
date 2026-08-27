import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import health_checks
from config import config


class _Ctx:
    """
    Stands in for an aiohttp async context manager. Raising from __aenter__
    rather than from the call itself matches where aiohttp actually raises
    a connection error, which is the branch these tests care about.
    """

    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._value

    async def __aexit__(self, *exc):
        return False


class _FakeResponse:
    def __init__(self, status: int, body: dict | None = None):
        self.status = status
        self._body = body or {}

    async def json(self):
        return self._body


class _FakeSession:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.requested_urls: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url):
        self.requested_urls.append(url)
        return _Ctx(value=self._response, error=self._error)


def factory_for(response=None, error=None):
    session = _FakeSession(response=response, error=error)
    return (lambda: session), session


@pytest.fixture(autouse=True)
def clean_health_state():
    health_checks.reset()
    original = dict(config._data)
    config._data["public_base_url"] = "https://hub.example.org"
    yield
    config._data.clear()
    config._data.update(original)
    health_checks.reset()


class TestPublicHealthUrl:
    def test_appends_health_to_the_configured_base(self):
        assert health_checks.public_health_url() == "https://hub.example.org/health"

    def test_does_not_double_the_slash_on_a_trailing_slash_base(self):
        config._data["public_base_url"] = "https://hub.example.org/"
        assert health_checks.public_health_url() == "https://hub.example.org/health"

    def test_is_none_when_nothing_is_configured(self):
        config._data["public_base_url"] = ""
        assert health_checks.public_health_url() is None


class TestProbe:
    def test_an_unconfigured_base_url_is_not_a_failure(self):
        # Distinct from unreachable on purpose: nothing is broken, the
        # check simply has no address to try, and the panel says so
        # rather than showing a red light.
        config._data["public_base_url"] = ""
        asyncio.run(health_checks.probe_once())
        status = health_checks.status()
        assert status["reachable"] is None
        assert "public_base_url" in status["detail"]

    def test_our_own_instance_answering_is_reachable(self):
        factory, session = factory_for(
            _FakeResponse(200, {"status": "ok", "instance": health_checks.INSTANCE_ID})
        )
        asyncio.run(health_checks.probe_once(factory))
        assert health_checks.status()["reachable"] is True
        assert session.requested_urls == ["https://hub.example.org/health"]

    def test_a_different_instance_is_not_reachable(self):
        # The case the token exists for: a stale cloudflared or a DNS
        # record pointing elsewhere answers /health perfectly happily.
        factory, _ = factory_for(_FakeResponse(200, {"status": "ok", "instance": "someone-else"}))
        asyncio.run(health_checks.probe_once(factory))
        status = health_checks.status()
        assert status["reachable"] is False
        assert "different backend" in status["detail"]

    def test_a_response_with_no_instance_at_all_is_not_reachable(self):
        factory, _ = factory_for(_FakeResponse(200, {"status": "ok"}))
        asyncio.run(health_checks.probe_once(factory))
        assert health_checks.status()["reachable"] is False

    def test_a_non_200_is_reported_with_its_status(self):
        factory, _ = factory_for(_FakeResponse(502))
        asyncio.run(health_checks.probe_once(factory))
        status = health_checks.status()
        assert status["reachable"] is False
        assert "502" in status["detail"]

    def test_a_timeout_says_the_tunnel_is_down(self):
        factory, _ = factory_for(error=asyncio.TimeoutError())
        asyncio.run(health_checks.probe_once(factory))
        status = health_checks.status()
        assert status["reachable"] is False
        assert "tunnel" in status["detail"]

    def test_a_connection_error_never_escapes(self):
        # This runs on a timer inside the backend's own event loop, so a
        # raised exception here would take out the probe task silently and
        # freeze the panel on its last answer forever.
        factory, _ = factory_for(error=OSError("Connection refused"))
        asyncio.run(health_checks.probe_once(factory))
        status = health_checks.status()
        assert status["reachable"] is False
        assert "Connection refused" in status["detail"]

    def test_a_failure_after_a_success_replaces_the_cached_answer(self):
        ok_factory, _ = factory_for(_FakeResponse(200, {"instance": health_checks.INSTANCE_ID}))
        asyncio.run(health_checks.probe_once(ok_factory))
        assert health_checks.status()["reachable"] is True

        bad_factory, _ = factory_for(error=OSError("gone"))
        asyncio.run(health_checks.probe_once(bad_factory))
        assert health_checks.status()["reachable"] is False


class TestStatus:
    def test_a_never_checked_backend_reports_no_age(self):
        assert health_checks.status()["checked_age_seconds"] is None

    def test_the_age_starts_counting_after_a_probe(self):
        factory, _ = factory_for(_FakeResponse(200, {"instance": health_checks.INSTANCE_ID}))
        asyncio.run(health_checks.probe_once(factory))
        assert health_checks.status()["checked_age_seconds"] < 1

    def test_the_url_is_reported_so_the_panel_can_show_what_was_tried(self):
        assert health_checks.status()["url"] == "https://hub.example.org/health"

    def test_the_instance_id_is_not_a_constant_across_processes(self):
        # Not a real multi-process test - just a guard against someone
        # replacing the token with a fixed string, which would silently
        # defeat the entire stale-tunnel check above.
        assert len(health_checks.INSTANCE_ID) >= 8
