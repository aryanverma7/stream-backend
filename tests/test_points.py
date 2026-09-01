import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import points
import points_local
from config import config


@pytest.fixture
def local_ledger(tmp_path, monkeypatch):
    """
    Points the ledger at a throwaway file and selects the local backend.
    Clears the module's in-memory copy on both sides of the test, so one
    test's balances can never be read by the next.
    """
    ledger = tmp_path / "points_local.json"
    monkeypatch.setattr(
        config, "_data", {"points_backend": "local", "points_local_file": str(ledger)}
    )
    points_local.reset_cache()
    yield ledger
    points_local.reset_cache()


class TestBackendName:
    def test_defaults_to_the_streamlabs_api(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert points.backend_name() == "api"

    def test_reads_the_configured_backend(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "local"})
        assert points.backend_name() == "local"

    def test_an_unrecognized_backend_falls_back_rather_than_raising(self, monkeypatch):
        """
        This is read on every points call, including from a chat command
        handler. A typo in config.json should cost a log line, not the
        roulette.
        """
        monkeypatch.setattr(config, "_data", {"points_backend": "streamlabs"})
        assert points.backend_name() == "api"


class TestLocalLedgerBalances:
    @pytest.mark.asyncio
    async def test_an_unknown_user_has_zero_not_an_error(self, local_ledger):
        """
        roulette.trigger_roulette() turns an exception here into "Couldn't
        verify your points balance right now" and a zero into "Need 500
        points, you have 0". The second is the true answer for somebody
        who has never been granted anything.
        """
        assert await points.get_user_points("neverseen") == 0

    @pytest.mark.asyncio
    async def test_granting_then_reading_returns_the_granted_amount(self, local_ledger):
        assert await points.grant_points("viewer", 500) == 500
        assert await points.get_user_points("viewer") == 500

    @pytest.mark.asyncio
    async def test_grants_accumulate(self, local_ledger):
        await points.grant_points("viewer", 500)
        assert await points.grant_points("viewer", 250) == 750

    @pytest.mark.asyncio
    async def test_subtracting_reduces_the_balance(self, local_ledger):
        await points.grant_points("viewer", 500)
        await points.subtract_points("viewer", 200)
        assert await points.get_user_points("viewer") == 300

    @pytest.mark.asyncio
    async def test_spending_the_exact_balance_is_allowed(self, local_ledger):
        await points.grant_points("viewer", 500)
        await points.subtract_points("viewer", 500)
        assert await points.get_user_points("viewer") == 0

    @pytest.mark.asyncio
    async def test_overspending_raises_rather_than_going_negative(self, local_ledger):
        """
        roulette.py checks the balance before spending, so this should
        never fire in practice - but a ledger that can go negative would
        let a race past that check hand somebody a debt.
        """
        await points.grant_points("viewer", 100)
        with pytest.raises(ValueError):
            await points.subtract_points("viewer", 500)
        assert await points.get_user_points("viewer") == 100

    @pytest.mark.asyncio
    async def test_usernames_are_case_insensitive(self, local_ledger):
        """
        Twitch hands us a login, YouTube hands us a display name. The same
        person typing !roulette twice must not end up with two balances.
        """
        await points.grant_points("DualBladeX", 500)
        assert await points.get_user_points("dualbladex") == 500
        await points.subtract_points("DUALBLADEX", 500)
        assert await points.get_user_points("DualBladeX") == 0


class TestLocalLedgerPersistence:
    @pytest.mark.asyncio
    async def test_a_grant_is_written_to_disk(self, local_ledger):
        await points.grant_points("viewer", 500)
        assert json.loads(local_ledger.read_text()) == {"viewer": 500}

    @pytest.mark.asyncio
    async def test_balances_survive_a_restart(self, local_ledger):
        await points.grant_points("viewer", 500)
        points_local.reset_cache()  # what a backend restart looks like
        assert await points.get_user_points("viewer") == 500

    @pytest.mark.asyncio
    async def test_a_missing_ledger_file_is_an_empty_one_not_an_error(self, local_ledger):
        assert not local_ledger.exists()
        assert await points.get_user_points("viewer") == 0

    @pytest.mark.asyncio
    async def test_an_unparseable_ledger_raises_rather_than_starting_from_zero(self, local_ledger):
        """
        Silently treating a corrupt file as empty would wipe every balance
        on disk the moment anything wrote back.
        """
        local_ledger.write_text("{not json")
        with pytest.raises(Exception):
            await points.get_user_points("viewer")

    @pytest.mark.asyncio
    async def test_a_ledger_that_is_not_an_object_is_rejected(self, local_ledger):
        local_ledger.write_text("[1, 2, 3]")
        with pytest.raises(ValueError):
            await points.get_user_points("viewer")


class TestBackendDispatch:
    @pytest.mark.asyncio
    async def test_the_local_backend_never_touches_the_network(self, local_ledger, monkeypatch):
        """
        The point of the switch is that Streamlabs is unreachable for us
        right now. If any of the three operations still opened a session,
        the local backend would 401 exactly like the API one.
        """
        def explode(*args, **kwargs):
            raise AssertionError("local backend must not open an HTTP session")

        monkeypatch.setattr(points.aiohttp, "ClientSession", explode)
        await points.grant_points("viewer", 500)
        await points.get_user_points("viewer")
        await points.subtract_points("viewer", 100)

    @pytest.mark.asyncio
    async def test_the_cloudbot_backend_is_reachable_through_the_dispatcher(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})
        called = {}

        async def fake_read(username, platform=""):
            called["username"] = username
            return 19

        monkeypatch.setattr(points.points_cloudbot, "get_user_points", fake_read)
        assert await points.get_user_points("someviewer") == 19
        assert called["username"] == "someviewer"

    @pytest.mark.asyncio
    async def test_the_api_backend_is_still_the_one_used_by_default(self, monkeypatch):
        """
        Flipping back to Streamlabs once its approval lands is a config
        edit, not a code change - so the default has to stay "api".
        """
        monkeypatch.setattr(config, "_data", {})
        called = {}

        async def fake_api_get(username):
            called["username"] = username
            return 1234

        monkeypatch.setattr(points, "_api_get_user_points", fake_api_get)
        assert await points.get_user_points("viewer") == 1234
        assert called["username"] == "viewer"


class TestTrySpend:
    """
    The primitive roulette.py charges with. It replaced a
    get_user_points-then-subtract_points pair because that pair is only
    implementable on backends that can read a balance, and the cloudbot
    backend cannot read one at all.
    """

    @pytest.mark.asyncio
    async def test_spends_when_the_balance_covers_it(self, local_ledger):
        await points.grant_points("viewer", 500)
        assert await points.try_spend("viewer", 200) == (True, None)
        assert await points.get_user_points("viewer") == 300

    @pytest.mark.asyncio
    async def test_reports_the_real_balance_when_short(self, local_ledger):
        """roulette turns this into "Need 500 points, you have 100"."""
        await points.grant_points("viewer", 100)
        assert await points.try_spend("viewer", 500) == (False, 100)

    @pytest.mark.asyncio
    async def test_a_refused_spend_takes_nothing(self, local_ledger):
        await points.grant_points("viewer", 100)
        await points.try_spend("viewer", 500)
        assert await points.get_user_points("viewer") == 100

    @pytest.mark.asyncio
    async def test_spending_the_whole_balance_is_allowed(self, local_ledger):
        await points.grant_points("viewer", 500)
        assert await points.try_spend("viewer", 500) == (True, None)
        assert await points.get_user_points("viewer") == 0

    @pytest.mark.asyncio
    async def test_a_user_with_no_ledger_entry_is_short_not_an_error(self, local_ledger):
        assert await points.try_spend("neverseen", 50) == (False, 0)

    @pytest.mark.asyncio
    async def test_usernames_are_case_insensitive(self, local_ledger):
        await points.grant_points("DualBladeX", 500)
        assert await points.try_spend("DUALBLADEX", 500) == (True, None)
        assert await points.get_user_points("dualbladex") == 0

    @pytest.mark.asyncio
    async def test_dispatches_to_the_cloudbot_backend(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})

        async def fake_try_spend(username, amount, platform=""):
            return False, 120

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)
        assert await points.try_spend("someviewer", 500) == (False, 120)

    @pytest.mark.asyncio
    async def test_dispatches_to_the_streamlabs_api_backend(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "api"})
        calls = []

        async def fake_read(username):
            return 700

        async def fake_subtract(username, amount):
            calls.append((username, amount))

        monkeypatch.setattr(points, "_api_get_user_points", fake_read)
        monkeypatch.setattr(points, "_api_subtract_points", fake_subtract)

        assert await points.try_spend("viewer", 500) == (True, None)
        assert calls == [("viewer", 500)]

    @pytest.mark.asyncio
    async def test_the_api_backend_does_not_subtract_when_short(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"points_backend": "api"})
        calls = []

        async def fake_read(username):
            return 100

        async def fake_subtract(username, amount):
            calls.append((username, amount))

        monkeypatch.setattr(points, "_api_get_user_points", fake_read)
        monkeypatch.setattr(points, "_api_subtract_points", fake_subtract)

        assert await points.try_spend("viewer", 500) == (False, 100)
        assert calls == []


class TestUnknownUserTranslation:
    @pytest.mark.asyncio
    async def test_a_cloudbot_not_found_becomes_a_backend_neutral_error(self, monkeypatch):
        """
        roulette.py catches points.UnknownUser, so it must not have to
        import a backend module or know which one is live.
        """
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})

        async def fake_try_spend(username, amount, platform=""):
            raise points.points_cloudbot.CloudbotUserNotFound("someviewer")

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)

        with pytest.raises(points.UnknownUser):
            await points.try_spend("someviewer", 500)

    @pytest.mark.asyncio
    async def test_other_cloudbot_failures_are_left_alone(self, monkeypatch):
        """A timeout is an outage, not an unknown viewer."""
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})

        async def fake_try_spend(username, amount, platform=""):
            raise TimeoutError("no answer")

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)

        with pytest.raises(TimeoutError):
            await points.try_spend("someviewer", 500)


class TestPlatformIsPassedThrough:
    @pytest.mark.asyncio
    async def test_try_spend_hands_the_platform_to_the_cloudbot_backend(self, monkeypatch):
        """
        Cloudbot resolves a username only in the chat the command was
        typed in, so the viewer's platform has to survive the dispatcher.
        """
        monkeypatch.setattr(config, "_data", {"points_backend": "cloudbot"})
        seen = {}

        async def fake_try_spend(username, amount, platform=""):
            seen["platform"] = platform
            return True, None

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)
        await points.try_spend("someviewer", 350, platform="youtube")

        assert seen["platform"] == "youtube"

    @pytest.mark.asyncio
    async def test_the_local_backend_ignores_it(self, local_ledger):
        """Only Cloudbot keeps a wallet per platform."""
        await points.grant_points("viewer", 500)

        assert await points.try_spend("viewer", 200, platform="youtube") == (True, None)
        assert await points.get_user_points("viewer", platform="twitch") == 300


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class _FakeSession:
    """
    Records the URL, params and headers of whatever the points module
    sends, so the wire format can be asserted without a network.
    """

    def __init__(self, calls, payload, error=None):
        self._calls = calls
        self._payload = payload
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def request(self, method, url, params=None, json=None, headers=None):
        self._calls.append(
            {"method": method, "url": url, "params": params, "body": json, "headers": headers}
        )
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._payload)


class TestTheStreamlabsWireFormat:
    """
    The URLs themselves, pinned.

    Every other test in this file mocks at the function level, which is
    what let a wrong endpoint sit here unnoticed for the whole time the
    Loyalty API was 401ing: the read was written against
    /points/user_points, which is the channel leaderboard - a page of 100
    users matched on a partial name - rather than /points, which is one
    user's balance. It could never 404, because it never got past auth.
    """

    def _install(self, monkeypatch, payload, error=None):
        calls = []
        monkeypatch.setattr(config, "_data", {
            "points_backend": "api",
            "streamlabs_access_token": "tok-123",
            "streamlabs_channel": "dualbladex",
        })
        # Accepts the timeout kwarg the real session is now always given -
        # a fake that refused it would pass while the real call failed.
        monkeypatch.setattr(
            points.aiohttp, "ClientSession", lambda timeout=None: _FakeSession(calls, payload, error)
        )
        return calls

    @pytest.mark.asyncio
    async def test_a_balance_read_goes_to_points_not_the_leaderboard(self, monkeypatch):
        calls = self._install(monkeypatch, {"points": 1980, "username": "someviewer"})
        assert await points.get_user_points("someviewer") == 1980

        assert calls[0]["url"] == "https://streamlabs.com/api/v2.0/points"
        assert not calls[0]["url"].endswith("/user_points")
        assert calls[0]["params"] == {"username": "someviewer", "channel": "dualbladex"}

    @pytest.mark.asyncio
    async def test_the_token_goes_in_a_bearer_header(self, monkeypatch):
        """v2.0 does not accept the access token as a query parameter at all."""
        calls = self._install(monkeypatch, {"points": 10})
        await points.get_user_points("someviewer")

        assert calls[0]["headers"]["Authorization"] == "Bearer tok-123"
        assert "access_token" not in (calls[0]["params"] or {})

    @pytest.mark.asyncio
    async def test_subtract_carries_the_channel(self, monkeypatch):
        calls = self._install(monkeypatch, {"points": 0})
        await points.subtract_points("someviewer", 350)

        assert calls[0]["url"] == "https://streamlabs.com/api/v2.0/points/subtract"
        assert calls[0]["body"] == {"username": "someviewer", "channel": "dualbladex", "points": 350}

    @pytest.mark.asyncio
    async def test_the_absolute_set_does_not_carry_a_channel(self, monkeypatch):
        """
        Confirmed against the reference docs: user_point_edit takes only
        username and points, and `points` REPLACES the balance rather than
        adding to it - which is why granting is read, add, set.
        """
        calls = self._install(monkeypatch, {"points": 500})
        await points.grant_points("someviewer", 100)

        read, write = calls[0], calls[1]
        assert read["method"] == "GET"
        assert write["url"] == "https://streamlabs.com/api/v2.0/points/user_point_edit"
        assert write["body"] == {"username": "someviewer", "points": 600}
        assert "channel" not in write["body"]

    @pytest.mark.asyncio
    async def test_a_spend_reads_first_so_a_refusal_can_say_the_balance(self, monkeypatch):
        calls = self._install(monkeypatch, {"points": 120})
        ok, balance = await points.try_spend("someviewer", 350)

        assert ok is False
        assert balance == 120
        # Nothing was taken - the refusal happens before any write.
        assert all(call["method"] == "GET" for call in calls)


class TestTheOutboundCallCannotHangForever:
    """
    The dashboard's points routes are the only handlers in this backend
    that wait on a third party, and a hang there does not look like a
    hang - aiohttp's default allows five minutes, and the Cloudflare
    tunnel in front of this gives up long before that and serves its own
    HTML error page. The browser then reports "JSON.parse: unexpected
    character at line 1 column 1" while the backend is up, healthy, and
    answering /health perfectly. Nothing connects the two.
    """

    def test_every_session_carries_a_timeout(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert points._timeout().total == points.DEFAULT_REQUEST_TIMEOUT_SECONDS

    def test_the_timeout_is_config_overridable(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"streamlabs_api_timeout_seconds": 3})
        assert points._timeout().total == 3

    def test_a_nonsense_timeout_falls_back_rather_than_raising(self, monkeypatch):
        """The config editor is a free-text JSON field; a typo must not take points down."""
        monkeypatch.setattr(config, "_data", {"streamlabs_api_timeout_seconds": "soon"})
        assert points._timeout().total == points.DEFAULT_REQUEST_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_a_hang_becomes_a_readable_error_not_a_blank_one(self, monkeypatch):
        """
        asyncio.TimeoutError's str() is the empty string, and the
        dashboard renders str(e) - so without the translation the panel
        would show a 502 with no message at all. True, and useless.
        """
        calls = []
        monkeypatch.setattr(config, "_data", {
            "points_backend": "api",
            "streamlabs_access_token": "tok-123",
            "streamlabs_channel": "dualbladex",
        })
        monkeypatch.setattr(
            points.aiohttp,
            "ClientSession",
            lambda timeout=None: _FakeSession(calls, {}, error=asyncio.TimeoutError()),
        )

        with pytest.raises(points.StreamlabsUnreachable) as caught:
            await points.get_user_points("someviewer")
        assert "did not answer" in str(caught.value)

    @pytest.mark.asyncio
    async def test_an_unreachable_ledger_is_never_treated_as_a_successful_spend(self, monkeypatch):
        """
        The important half. try_spend raising must mean "not paid" - a
        viewer charged for a roulette that never opened is the failure
        this whole path exists to avoid.
        """
        calls = []
        monkeypatch.setattr(config, "_data", {
            "points_backend": "api",
            "streamlabs_access_token": "tok-123",
            "streamlabs_channel": "dualbladex",
        })
        monkeypatch.setattr(
            points.aiohttp,
            "ClientSession",
            lambda timeout=None: _FakeSession(calls, {}, error=asyncio.TimeoutError()),
        )

        with pytest.raises(points.StreamlabsUnreachable):
            await points.try_spend("someviewer", 350)
        # It raised on the READ, so nothing was ever subtracted.
        assert all(call["method"] == "GET" for call in calls)
