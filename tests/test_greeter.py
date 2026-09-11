import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import greeter
from config import config

ENABLED = {
    "greeter_enabled": True,
    "discord_invite_url": "https://discord.gg/example",
    "greeter_cooldown_seconds": 45,
}


@pytest.fixture(autouse=True)
def clean_greeter():
    """
    The seen list is module-level and cached, so it has to be dropped
    between tests or one test's greeting suppresses the next's. Takes no
    other fixture: an autouse fixture that does is fine under pytest and
    not under the minimal runner this suite is also exercised with.
    """
    greeter.reset()
    yield
    greeter.reset()


def enabled(tmp_path, **extra):
    return {**ENABLED, "greeter_seen_file": str(tmp_path / "seen.json"), **extra}


def chat_event(username="newviewer", platform="Twitch", text="hey", **data):
    return {
        "event": {"source": platform, "type": "ChatMessage"},
        "data": {"user": {"login": username, "name": username.title()}, "text": text, **data},
    }


class TestWhoGetsGreeted:
    def test_a_name_never_seen_before(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        assert greeter.should_greet("twitch", "newviewer", time.time()) is True

    def test_someone_already_greeted_is_not_greeted_again(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        greeter.remember("twitch", "regular")
        assert greeter.should_greet("twitch", "regular", time.time()) is False

    def test_the_same_handle_on_the_other_platform_is_a_different_person(self, monkeypatch, tmp_path):
        """
        Not reliably the same human, and greeting someone twice costs far
        less than never greeting a real newcomer whose name collides.
        """
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        greeter.remember("twitch", "someone")
        assert greeter.should_greet("youtube", "someone", time.time()) is True

    def test_ignored_users_are_never_greeted(self, monkeypatch, tmp_path):
        """The bot account and the streamer are in chat constantly."""
        monkeypatch.setattr(config, "_data", enabled(tmp_path, greeter_ignore_users=["pinkuthagoat"]))
        assert greeter.should_greet("twitch", "PinkuThaGoat", time.time()) is False

    def test_off_by_default(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", {"greeter_seen_file": str(tmp_path / "seen.json")})
        assert greeter.is_enabled() is False
        assert greeter.should_greet("twitch", "newviewer", time.time()) is False

    def test_no_invite_link_means_no_greeting(self, monkeypatch, tmp_path):
        """Enabled with nothing to link to would post "welcome in! Come hang out in the Discord: "."""
        monkeypatch.setattr(
            config, "_data", {"greeter_enabled": True, "greeter_seen_file": str(tmp_path / "seen.json")}
        )
        assert greeter.is_enabled() is False


class TestTheRaidGuard:
    """
    Forty first-time chatters in ten seconds is a raid, and forty invite
    links would be the worst possible welcome.
    """

    def test_a_second_newcomer_inside_the_cooldown_is_not_greeted(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        now = time.time()
        monkeypatch.setattr(greeter, "_last_greeted_at", now)
        assert greeter.should_greet("twitch", "newviewer", now + 5) is False

    def test_and_is_greeted_once_the_window_passes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        now = time.time()
        monkeypatch.setattr(greeter, "_last_greeted_at", now)
        assert greeter.should_greet("twitch", "newviewer", now + 46) is True

    @pytest.mark.asyncio
    async def test_someone_skipped_by_the_cooldown_is_still_recorded(self, monkeypatch, tmp_path):
        """
        Otherwise they get greeted an hour later, when the cooldown has
        long expired and they are no longer new - which is stranger than
        not greeting them at all.
        """
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        monkeypatch.setattr(greeter, "_last_greeted_at", time.time())
        send = AsyncMock()
        monkeypatch.setattr(greeter, "_load", greeter._load)
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await greeter.handle_chat_command(chat_event(username="raider"))

        send.assert_not_awaited()
        assert greeter.has_seen("twitch", "raider") is True


class TestTheGreeting:
    def test_carries_the_invite(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        assert "https://discord.gg/example" in greeter.greeting_for("NewViewer")
        assert greeter.greeting_for("NewViewer").startswith("@NewViewer ")

    def test_the_wording_is_config_overridable(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            config, "_data", enabled(tmp_path, greeter_message="hi! discord: {discord}")
        )
        assert greeter.greeting_for("x") == "@x hi! discord: https://discord.gg/example"

    @pytest.mark.asyncio
    async def test_a_first_message_is_greeted_end_to_end(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        send = AsyncMock()
        import streamerbot_client

        monkeypatch.setattr(streamerbot_client.streamerbot, "send_chat_message", send)

        await greeter.handle_chat_command(chat_event())

        send.assert_awaited_once()
        assert "discord.gg/example" in send.await_args[0][0]
        # And never twice.
        send.reset_mock()
        await greeter.handle_chat_command(chat_event())
        send.assert_not_awaited()


class TestPersistence:
    def test_the_list_survives_a_restart(self, monkeypatch, tmp_path):
        """
        A restart mid-stream must not re-welcome the whole chat, which is
        exactly the moment it would look broken.
        """
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        greeter.remember("twitch", "regular")

        greeter.reset()  # what a restart looks like
        assert greeter.has_seen("twitch", "regular") is True

    def test_a_corrupt_file_does_not_take_chat_down(self, monkeypatch, tmp_path):
        path = tmp_path / "seen.json"
        path.write_text("{not json")
        monkeypatch.setattr(config, "_data", enabled(tmp_path))

        assert greeter.has_seen("twitch", "anyone") is False


class TestTheTwitchFirstMessageFlag:
    """
    Twitch sends `first-msg`; YouTube sends nothing of the kind. Building
    on the flag alone would mean the feature silently worked on half the
    audience, so it can only ever ADD a greeting.
    """

    def test_the_flag_greets_someone_the_backend_has_seen(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        greeter.remember("twitch", "known")
        assert greeter.should_greet("twitch", "known", time.time(), first_message_flag=True) is True

    def test_its_absence_never_suppresses_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "_data", enabled(tmp_path))
        assert greeter.should_greet("youtube", "brandnew", time.time(), first_message_flag=False) is True
