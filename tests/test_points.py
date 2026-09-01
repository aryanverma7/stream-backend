import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import points
from config import config


class TestTrySpend:
    """
    The primitive roulette.py charges with. It replaced a
    get_user_points-then-subtract_points pair because that pair needs a
    readable balance and Cloudbot has none - affordability is decided by
    spending and seeing how much came back.
    """


    @pytest.mark.asyncio
    async def test_dispatches_to_the_cloudbot_backend(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})

        async def fake_try_spend(username, amount, platform=""):
            return False, 120

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)
        assert await points.try_spend("someviewer", 500) == (False, 120)


class TestUnknownUserTranslation:
    @pytest.mark.asyncio
    async def test_a_cloudbot_not_found_becomes_a_backend_neutral_error(self, monkeypatch):
        """
        roulette.py catches points.UnknownUser, so it must not have to
        import points_cloudbot and catch its own exception type.
        """
        monkeypatch.setattr(config, "_data", {})

        async def fake_try_spend(username, amount, platform=""):
            raise points.points_cloudbot.CloudbotUserNotFound("someviewer")

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)

        with pytest.raises(points.UnknownUser):
            await points.try_spend("someviewer", 500)

    @pytest.mark.asyncio
    async def test_other_cloudbot_failures_are_left_alone(self, monkeypatch):
        """A timeout is an outage, not an unknown viewer."""
        monkeypatch.setattr(config, "_data", {})

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
        monkeypatch.setattr(config, "_data", {})
        seen = {}

        async def fake_try_spend(username, amount, platform=""):
            seen["platform"] = platform
            return True, None

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)
        await points.try_spend("someviewer", 350, platform="youtube")

        assert seen["platform"] == "youtube"


class TestSpendsAndGrantsAreSerialised:
    """
    With one ledger left, the locks are most of what this module still
    contributes, and they are not decoration.

    Cloudbot is reached by posting a command into chat and waiting for its
    reply. Two spends in flight at once means two !removepoints and two
    replies, and points_cloudbot matches a reply to the command that
    caused it by (platform, username) - so two overlapping commands for
    the same viewer can be answered in either order with no way to tell
    which confirmation belongs to which. Serialising is what makes the
    matching sound.
    """

    @pytest.mark.asyncio
    async def test_two_spends_never_overlap(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        active = 0
        overlapped = False

        async def fake_try_spend(username, amount, platform=""):
            nonlocal active, overlapped
            active += 1
            if active > 1:
                overlapped = True
            # A real await, so the second caller genuinely gets a chance to
            # run here if the lock is not holding it back.
            await asyncio.sleep(0)
            active -= 1
            return True, None

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fake_try_spend)
        await asyncio.gather(*(points.try_spend("someviewer", 50) for _ in range(5)))

        assert overlapped is False

    @pytest.mark.asyncio
    async def test_two_grants_never_overlap(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        active = 0
        overlapped = False

        async def fake_grant(username, amount, platform=""):
            nonlocal active, overlapped
            active += 1
            if active > 1:
                overlapped = True
            await asyncio.sleep(0)
            active -= 1
            return None

        monkeypatch.setattr(points.points_cloudbot, "grant_points", fake_grant)
        await asyncio.gather(*(points.grant_points("someviewer", 50) for _ in range(5)))

        assert overlapped is False

    @pytest.mark.asyncio
    async def test_a_raising_spend_releases_the_lock(self, monkeypatch):
        """
        Otherwise one Cloudbot timeout deadlocks every future spend, and
        the roulette stops taking payment until the backend is restarted.
        """
        monkeypatch.setattr(config, "_data", {})

        async def boom(username, amount, platform=""):
            raise TimeoutError("no answer")

        monkeypatch.setattr(points.points_cloudbot, "try_spend", boom)
        with pytest.raises(TimeoutError):
            await points.try_spend("someviewer", 50)

        async def fine(username, amount, platform=""):
            return True, None

        monkeypatch.setattr(points.points_cloudbot, "try_spend", fine)
        assert await points.try_spend("someviewer", 50) == (True, None)
