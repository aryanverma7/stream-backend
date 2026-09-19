import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import discord_live
from config import config

ENABLED = {
    "discord_live_enabled": True,
    "discord_webhook_url": "https://discord.com/api/webhooks/1/abc",
    "twitch_channel": "dualbladex",
    "discord_live_cooldown_minutes": 60,
}


class FakeResponse:
    def __init__(self, status=204, body=""):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records what was posted. Same injected-factory shape health_checks uses."""

    def __init__(self, status=204, body=""):
        self.status = status
        self.body = body
        self.posts = []

    def post(self, url, json=None, data=None):
        self.posts.append((url, json if json is not None else data))
        return FakeResponse(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def clean_state():
    """
    Takes no other fixture on purpose - an autouse fixture that does is fine
    under pytest and not under the minimal runner this suite also runs on.
    Every test calls _enable() for its own config.
    """
    discord_live.reset()
    yield
    discord_live.reset()


def _enable(monkeypatch, tmp_path, **extra):
    monkeypatch.setattr(config, "_data", dict(ENABLED, discord_live_state_file=str(tmp_path / "s.json"), **extra))
    discord_live.reset()


# ---------- the dedupe ----------

def test_disabled_without_webhook(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, discord_webhook_url="")
    assert discord_live.is_enabled() is False
    assert discord_live.should_announce("abc", time.time()) is False


def test_first_signal_announces(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    assert discord_live.should_announce("stream-1", time.time()) is True


@pytest.mark.asyncio
async def test_same_stream_id_is_suppressed(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    assert await discord_live.announce(stream_id="stream-1", session_factory=lambda: session) is True
    assert await discord_live.announce(stream_id="stream-1", session_factory=lambda: session) is False
    assert len(session.posts) == 1


@pytest.mark.asyncio
async def test_second_broadcast_inside_cooldown_is_suppressed(monkeypatch, tmp_path):
    """A stream that drops and returns is one session to everyone reading Discord."""
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    await discord_live.announce(stream_id="stream-1", session_factory=lambda: session)
    assert await discord_live.announce(stream_id="stream-2", session_factory=lambda: session) is False


@pytest.mark.asyncio
async def test_second_broadcast_after_cooldown_announces(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, discord_live_cooldown_minutes=0)
    session = FakeSession()
    await discord_live.announce(stream_id="stream-1", session_factory=lambda: session)
    assert await discord_live.announce(stream_id="stream-2", session_factory=lambda: session) is True
    assert len(session.posts) == 2


@pytest.mark.asyncio
async def test_state_survives_a_restart(monkeypatch, tmp_path):
    """The restart is exactly when a naive announcer re-announces."""
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    await discord_live.announce(stream_id="stream-1", session_factory=lambda: session)
    discord_live.reset()                      # a fresh process
    assert discord_live.should_announce("stream-1", time.time()) is False


@pytest.mark.asyncio
async def test_a_failed_post_is_not_remembered(monkeypatch, tmp_path):
    """An announcement that never posted has not been made."""
    _enable(monkeypatch, tmp_path)
    bad = FakeSession(status=500, body="nope")
    assert await discord_live.announce(stream_id="stream-1", session_factory=lambda: bad) is False
    good = FakeSession()
    assert await discord_live.announce(stream_id="stream-1", session_factory=lambda: good) is True


# ---------- the message ----------

def test_payload_substitutes_and_links(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    p = discord_live.build_payload("Radiant grind", "VALORANT", "https://twitch.tv/dbx", "DualBladeX")
    assert "DualBladeX" in p["content"] and "Radiant grind" in p["content"]
    assert p["embeds"][0]["url"] == "https://twitch.tv/dbx"
    assert p["embeds"][0]["description"] == "Playing VALORANT"


def test_a_stream_title_cannot_ping_the_server(monkeypatch, tmp_path):
    """Discord honours mentions in content by default; the title is not ours."""
    _enable(monkeypatch, tmp_path)
    p = discord_live.build_payload("@everyone free subs", "", "", "DBX")
    assert p["allowed_mentions"] == {"parse": []}


def test_configured_role_is_allowed_to_ping(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, discord_live_role_mention="<@&12345>")
    p = discord_live.build_payload("t", "", "", "n")
    assert p["content"].startswith("<@&12345>")
    assert p["allowed_mentions"] == {"roles": ["12345"]}


# ---------- source 1: the poll ----------

@pytest.mark.asyncio
async def test_poll_announces_when_live(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    monkeypatch.setattr(discord_live, "announce", _recording_announce(session))

    async def live(_channel):
        return {"id": "42", "title": "grind", "game_name": "VALORANT", "user_name": "DualBladeX"}

    assert await discord_live.poll_once(stream_info=live) is True


@pytest.mark.asyncio
async def test_poll_says_nothing_when_offline(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)

    async def offline(_channel):
        return None

    assert await discord_live.poll_once(stream_info=offline) is False


@pytest.mark.asyncio
async def test_poll_survives_an_api_failure(monkeypatch, tmp_path):
    """Twitch being unreachable is not a reason to take the backend down."""
    _enable(monkeypatch, tmp_path)

    async def boom(_channel):
        raise RuntimeError("twitch is down")

    assert await discord_live.poll_once(stream_info=boom) is False


def _recording_announce(session):
    async def fake(**kwargs):
        return await _real_announce(session, **kwargs)
    return fake


async def _real_announce(session, **kwargs):
    kwargs["session_factory"] = lambda: session
    return await _ANNOUNCE(**kwargs)


_ANNOUNCE = discord_live.announce


# ---------- source 2: Streamer.bot ----------

@pytest.mark.asyncio
async def test_streamerbot_golive_event_announces(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    monkeypatch.setattr(discord_live, "announce", _recording_announce(session))
    await discord_live.handle_streamerbot_event({
        "event": {"source": "Twitch", "type": "StreamOnline"},
        "data": {"id": "99", "title": "back on"},
    })
    assert len(session.posts) == 1


@pytest.mark.asyncio
async def test_an_unrelated_event_is_ignored(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    session = FakeSession()
    monkeypatch.setattr(discord_live, "announce", _recording_announce(session))
    await discord_live.handle_streamerbot_event({
        "event": {"source": "Twitch", "type": "ChatMessage"},
        "data": {"message": {"message": "hello"}},
    })
    assert session.posts == []


@pytest.mark.asyncio
async def test_both_sources_announce_only_once(monkeypatch, tmp_path):
    """
    The whole reason two sources are allowed: they collapse to one post.
    Mirrors roulette's two buy-phase signals and its debounce.
    """
    _enable(monkeypatch, tmp_path)
    session = FakeSession()

    async def live(_channel):
        return {"id": "77", "title": "grind", "game_name": "VALORANT", "user_name": "DBX"}

    monkeypatch.setattr(discord_live, "announce", _recording_announce(session))
    await discord_live.poll_once(stream_info=live)
    await discord_live.handle_streamerbot_event({
        "event": {"source": "Twitch", "type": "StreamOnline"}, "data": {"id": "77"},
    })
    assert len(session.posts) == 1


def test_embed_carries_the_stream_thumbnail(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    p = discord_live.build_payload("t", "", "https://twitch.tv/x", "n", "https://cdn/thumb.jpg?t=1")
    assert p["embeds"][0]["image"] == {"url": "https://cdn/thumb.jpg?t=1"}


def test_configured_image_beats_the_auto_screenshot(monkeypatch, tmp_path):
    """Twitch's own thumbnail is often a black frame at the moment of going live."""
    _enable(monkeypatch, tmp_path, discord_live_image_url="https://mine/thumb.png")
    p = discord_live.build_payload("t", "", "https://twitch.tv/x", "n", "https://cdn/auto.jpg")
    assert p["embeds"][0]["image"] == {"url": "https://mine/thumb.png"}


@pytest.mark.asyncio
async def test_a_local_thumbnail_is_uploaded_with_the_post(monkeypatch, tmp_path):
    """No hosting step: overwrite one file you already export to."""
    thumb = tmp_path / "thumb.jpg"
    thumb.write_bytes(b"jpegbytes")
    _enable(monkeypatch, tmp_path, discord_live_image_file=str(thumb),
            discord_live_image_url="https://ignored/x.png")
    session = FakeSession()
    assert await discord_live.announce(url="https://twitch.tv/x", stream_id="s1",
                                       session_factory=lambda: session) is True
    sent = session.posts[0][1]
    assert not isinstance(sent, dict), "a file post must be multipart, not json"


def test_attachment_beats_the_configured_url(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, discord_live_image_url="https://ignored/x.png")
    p = discord_live.build_payload("t", "", "https://twitch.tv/x", "n", "attachment://thumb.jpg")
    assert p["embeds"][0]["image"] == {"url": "attachment://thumb.jpg"}


@pytest.mark.asyncio
async def test_a_missing_thumbnail_file_does_not_block_the_post(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path, discord_live_image_file=str(tmp_path / "gone.jpg"))
    session = FakeSession()
    assert await discord_live.announce(url="https://twitch.tv/x", stream_id="s1",
                                       session_factory=lambda: session) is True
