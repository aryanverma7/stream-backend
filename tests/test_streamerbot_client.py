import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch

import streamerbot_client


# ---------- Chat payload parsing ----------
#
# parse_chat_message() has to cope with more than one payload shape,
# because which one arrives depends on the Streamer.bot build installed on
# the gaming PC and this backend cannot see that from here. Each shape gets
# its own test rather than one parametrized case, so a regression names the
# shape it broke.

def test_parses_the_current_twitch_shape():
    event = {
        "event": {"source": "Twitch", "type": "ChatMessage"},
        "data": {"user": {"login": "someviewer", "name": "SomeViewer"}, "text": "!vandal"},
    }

    assert streamerbot_client.parse_chat_message(event) == {
        "platform": "twitch",
        "username": "someviewer",
        "display_name": "SomeViewer",
        "text": "!vandal",
    }


def test_parses_the_older_nested_message_shape():
    event = {
        "event": {"source": "Twitch", "type": "ChatMessage"},
        "data": {"message": {"username": "someviewer", "message": "hey chat"}},
    }

    parsed = streamerbot_client.parse_chat_message(event)
    assert parsed["username"] == "someviewer"
    assert parsed["text"] == "hey chat"
    assert parsed["platform"] == "twitch"


def test_parses_a_youtube_message_with_the_text_under_message():
    """YouTube's Message event has no published schema, so the text is read
    from whichever of the known keys is actually a string."""
    event = {
        "event": {"source": "YouTube", "type": "Message"},
        "data": {"user": {"name": "A Viewer"}, "message": "!roulette"},
    }

    parsed = streamerbot_client.parse_chat_message(event)
    assert parsed["platform"] == "youtube"
    assert parsed["username"] == "A Viewer"
    assert parsed["text"] == "!roulette"


def test_ignores_non_chat_events():
    assert streamerbot_client.parse_chat_message({"event": {"type": "Follow"}, "data": {}}) is None


def test_survives_a_chat_event_with_nothing_usable_in_it():
    parsed = streamerbot_client.parse_chat_message(
        {"event": {"source": "Twitch", "type": "ChatMessage"}, "data": {}}
    )
    assert parsed == {"platform": "twitch", "username": "", "display_name": "", "text": ""}


# ---------- Widget relay ----------

@pytest.mark.asyncio
async def test_forward_chat_broadcasts_twitch_message():
    event = {
        "event": {"source": "Twitch", "type": "ChatMessage"},
        "data": {"user": {"login": "someviewer", "name": "SomeViewer"}, "text": "hey chat"},
    }

    with patch("streamerbot_client.widget_hub") as mock_hub:
        mock_hub.broadcast = AsyncMock()
        await streamerbot_client.forward_chat_to_widgets(event)

        mock_hub.broadcast.assert_awaited_once_with(
            {
                "type": "chat_message",
                "platform": "twitch",
                "username": "someviewer",
                "message": "hey chat",
            },
            tag="chat",
        )


@pytest.mark.asyncio
async def test_forward_chat_ignores_non_chat_events():
    event = {"event": {"type": "Follow"}, "data": {}}

    with patch("streamerbot_client.widget_hub") as mock_hub:
        mock_hub.broadcast = AsyncMock()
        await streamerbot_client.forward_chat_to_widgets(event)

        mock_hub.broadcast.assert_not_awaited()


# ---------- Subscription ----------
#
# The reason this whole area has tests at all: Streamer.bot sends nothing
# until a Subscribe request is accepted, so a silent omission here looks
# exactly like a quiet chat.

@pytest.mark.asyncio
async def test_subscribe_asks_for_the_chat_events_by_default():
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._subscribe(ws)

    payload = ws.send_json.await_args.args[0]
    assert payload["request"] == "Subscribe"
    assert payload["events"] == {"Twitch": ["ChatMessage"], "YouTube": ["Message"]}
    assert payload["id"].startswith("subscribe")


@pytest.mark.asyncio
async def test_subscribe_honours_a_configured_event_set(monkeypatch):
    from config import config

    monkeypatch.setitem(config._data, "streamerbot_subscribe_events", {"Twitch": ["Cheer"]})
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._subscribe(ws)

    assert ws.send_json.await_args.args[0]["events"] == {"Twitch": ["Cheer"]}


def test_an_accepted_subscription_flips_is_subscribed():
    client = streamerbot_client.StreamerBotClient()
    assert client.is_subscribed is False

    client._handle_response({"status": "ok", "id": "subscribe-1", "events": {"Twitch": ["ChatMessage"]}})

    assert client.is_subscribed is True


def test_a_rejected_subscription_leaves_is_subscribed_false():
    client = streamerbot_client.StreamerBotClient()
    client._subscribed = True

    client._handle_response({"status": "error", "id": "subscribe-2"})

    assert client.is_subscribed is False


# ---------- Sending ----------

@pytest.mark.asyncio
async def test_send_chat_message_sends_a_sendmessage_request():
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()
    client._ws = ws

    assert await client.send_chat_message("hello", platform="twitch") is True

    payload = ws.send_json.await_args.args[0]
    assert payload["request"] == "SendMessage"
    assert payload["platform"] == "twitch"
    assert payload["message"] == "hello"
    assert payload["bot"] is True


@pytest.mark.asyncio
async def test_send_chat_message_reports_failure_when_disconnected():
    client = streamerbot_client.StreamerBotClient()
    client._ws = None

    assert await client.send_chat_message("hello") is False
