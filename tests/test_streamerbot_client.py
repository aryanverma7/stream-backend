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


@pytest.mark.asyncio
async def test_an_accepted_subscription_flips_is_subscribed():
    client = streamerbot_client.StreamerBotClient()
    assert client.is_subscribed is False

    await client._handle_response(
        AsyncMock(), {"status": "ok", "id": "subscribe-1", "events": {"Twitch": ["ChatMessage"]}}
    )

    assert client.is_subscribed is True


@pytest.mark.asyncio
async def test_a_rejected_subscription_leaves_is_subscribed_false():
    client = streamerbot_client.StreamerBotClient()
    client._subscribed = True

    await client._handle_response(AsyncMock(), {"status": "error", "id": "subscribe-2"})

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


# ---------- Authentication ----------
#
# Streamer.bot marks SendMessage as requiring authentication, and its
# Enforce option extends that to every request including Subscribe. So the
# handshake order - Hello, Authenticate, Subscribe - is load-bearing, and
# these tests pin the order as much as the hash.

def _sent(ws):
    """The requests handed to a mocked socket, in order."""
    return [call.args[0]["request"] for call in ws.send_json.await_args_list]


def test_the_authentication_hash_matches_the_documented_two_step_algorithm():
    import base64
    import hashlib

    password, salt, challenge = "hunter2", "c2FsdA==", "Y2hhbGxlbmdl"

    salted = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("utf-8")
    expected = base64.b64encode(
        hashlib.sha256((salted + challenge).encode("utf-8")).digest()
    ).decode("utf-8")

    assert streamerbot_client._authentication_hash(password, salt, challenge) == expected


def test_the_per_connection_challenge_is_actually_mixed_in():
    # An answer that ignored the challenge would be replayable onto any
    # later connection, which is the entire reason for the second step.
    one = streamerbot_client._authentication_hash("pw", "salt", "challenge-one")
    two = streamerbot_client._authentication_hash("pw", "salt", "challenge-two")
    assert one != two


def test_a_different_password_gives_a_different_answer():
    right = streamerbot_client._authentication_hash("right", "salt", "challenge")
    wrong = streamerbot_client._authentication_hash("wrong", "salt", "challenge")
    assert right != wrong


@pytest.mark.asyncio
async def test_a_hello_without_a_challenge_subscribes_immediately():
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._handle_hello(ws, {"request": "Hello", "info": {}})

    # None, not False: the server never asked, which is not a failure.
    assert client.is_authenticated is None
    assert _sent(ws) == ["Subscribe"]


@pytest.mark.asyncio
async def test_a_challenge_is_answered_before_anything_is_subscribed(monkeypatch):
    from config import config

    monkeypatch.setitem(config._data, "streamerbot_ws_password", "hunter2")
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._handle_hello(ws, {
        "request": "Hello",
        "authentication": {"salt": "c2FsdA==", "challenge": "Y2hhbGxlbmdl"},
    })

    assert _sent(ws) == ["Authenticate"]
    payload = ws.send_json.await_args.args[0]
    assert payload["id"].startswith("authenticate")
    assert payload["authentication"] == streamerbot_client._authentication_hash(
        "hunter2", "c2FsdA==", "Y2hhbGxlbmdl"
    )


@pytest.mark.asyncio
async def test_a_challenge_with_no_configured_password_still_asks_for_chat(monkeypatch):
    from config import config

    monkeypatch.setitem(config._data, "streamerbot_ws_password", "")
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._handle_hello(ws, {
        "request": "Hello",
        "authentication": {"salt": "c2FsdA==", "challenge": "Y2hhbGxlbmdl"},
    })

    assert client.is_authenticated is False
    assert _sent(ws) == ["Subscribe"]


@pytest.mark.asyncio
async def test_an_accepted_authentication_then_subscribes():
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._handle_response(ws, {"status": "ok", "id": "authenticate-1"})

    assert client.is_authenticated is True
    assert _sent(ws) == ["Subscribe"]


@pytest.mark.asyncio
async def test_a_rejected_authentication_subscribes_anyway_but_records_the_failure():
    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()

    await client._handle_response(ws, {"status": "error", "id": "authenticate-1"})

    # A wrong password with the server's Enforce option off still leaves
    # chat readable, which is most of the value - so ask regardless. The
    # false is what the dashboard warns on.
    assert client.is_authenticated is False
    assert _sent(ws) == ["Subscribe"]
