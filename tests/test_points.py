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

        async def fake_read(username):
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

        async def fake_try_spend(username, amount):
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
