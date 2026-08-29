import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import time

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
async def test_send_chat_message_can_speak_as_the_broadcaster_instead_of_a_bot():
    """
    `bot: true` needs a second account connected in Streamer.bot. If none
    is, the request is accepted and nothing is ever posted - which from
    this side is indistinguishable from a reply that worked. Being able
    to fall back to the broadcaster account has to be a config edit.
    """
    from config import config

    client = streamerbot_client.StreamerBotClient()
    ws = AsyncMock()
    client._ws = ws
    config._data = {"streamerbot_send_as_bot": False}
    try:
        assert await client.send_chat_message("hello") is True
    finally:
        config._data = {}

    assert ws.send_json.await_args.args[0]["bot"] is False


@pytest.mark.asyncio
async def test_send_chat_message_reports_failure_when_disconnected():
    client = streamerbot_client.StreamerBotClient()
    client._ws = None

    assert await client.send_chat_message("hello") is False


# ---------- Not obeying our own replies ----------
#
# Chat replies come straight back down the subscription as ordinary chat
# events. The !help reply used to open with "!roulette", so answering
# !help parsed as a !roulette trigger and charged the asker for a session
# they never asked for.


@pytest.mark.asyncio
async def test_a_message_we_just_sent_is_recognized_as_our_own():
    client = streamerbot_client.StreamerBotClient()
    client._ws = AsyncMock()

    await client.send_chat_message("Commands: !roulette (500 points) opens a vote")

    assert client._is_our_own_message("Commands: !roulette (500 points) opens a vote") is True


@pytest.mark.asyncio
async def test_a_message_we_never_sent_is_not_ours():
    client = streamerbot_client.StreamerBotClient()
    client._ws = AsyncMock()

    await client.send_chat_message("Roulette is open for 18s")

    assert client._is_our_own_message("!roulette") is False


@pytest.mark.asyncio
async def test_our_own_message_stops_being_ours_once_it_ages_out():
    """
    The window only has to outlast the round trip out to Streamer.bot and
    back. Holding sent text forever would eventually swallow a viewer who
    happened to type the same thing.
    """
    client = streamerbot_client.StreamerBotClient()
    client._ws = AsyncMock()
    await client.send_chat_message("hello")

    stale = time.monotonic() - streamerbot_client.SENT_MESSAGE_TTL_SECONDS - 1
    client._recently_sent[0] = (stale, "hello")

    assert client._is_our_own_message("hello") is False


@pytest.mark.asyncio
async def test_a_failed_send_is_not_remembered_as_ours():
    """Nothing reached chat, so nothing can come back."""
    client = streamerbot_client.StreamerBotClient()
    client._ws = None

    assert await client.send_chat_message("hello") is False
    assert client._is_our_own_message("hello") is False


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


# ---------- Listener fan-out ----------
#
# These pin the fix for a deadlock that made every Cloudbot points spend
# fail. Listeners used to be awaited one by one inside the socket read
# loop, so a listener that needed to READ the socket to finish could never
# finish: the cloudbot points backend charges a viewer by posting
# `!removepoints` in chat and waiting for Cloudbot's answer, and that
# answer arrives as the next chat event - on the loop that was blocked
# awaiting the handler waiting for it. Chat showed Cloudbot confirming the
# spend on time while the backend reported a timeout.

class TestListenerDispatch:
    @pytest.mark.asyncio
    async def test_dispatch_returns_without_waiting_for_listeners(self):
        import asyncio

        client = streamerbot_client.StreamerBotClient()
        started = asyncio.Event()
        release = asyncio.Event()

        async def blocks(event):
            started.set()
            await release.wait()

        client.on_event(blocks)
        client._dispatch({"event": {}})

        await asyncio.wait_for(started.wait(), 1)  # it did run
        release.set()

    @pytest.mark.asyncio
    async def test_a_listener_can_wait_for_an_event_that_arrives_later(self):
        """
        The exact shape the cloudbot bridge needs, and the exact shape the
        old sequential fan-out made impossible.
        """
        import asyncio

        client = streamerbot_client.StreamerBotClient()
        answered = asyncio.get_running_loop().create_future()
        seen = []

        async def asks_then_waits(event):
            if event.get("kind") == "question":
                seen.append(await asyncio.wait_for(answered, 1))

        async def answers(event):
            if event.get("kind") == "reply" and not answered.done():
                answered.set_result("confirmed")

        client.on_event(asks_then_waits)
        client.on_event(answers)

        client._dispatch({"kind": "question"})
        client._dispatch({"kind": "reply"})
        await asyncio.wait_for(answered, 1)
        await asyncio.sleep(0)

        assert seen == ["confirmed"]

    @pytest.mark.asyncio
    async def test_a_slow_listener_does_not_delay_the_others(self):
        import asyncio

        client = streamerbot_client.StreamerBotClient()
        release = asyncio.Event()
        fast_ran = asyncio.Event()

        async def slow(event):
            await release.wait()

        async def fast(event):
            fast_ran.set()

        client.on_event(slow)
        client.on_event(fast)
        client._dispatch({"event": {}})

        await asyncio.wait_for(fast_ran.wait(), 1)
        release.set()

    @pytest.mark.asyncio
    async def test_a_listener_that_raises_does_not_take_the_others_with_it(self):
        """
        An exception used to escape into the read loop and tear down the
        connection, so one bad payload dropped chat, the roulette and the
        widget relay together - and the reconnect looked like a network
        fault.
        """
        import asyncio

        client = streamerbot_client.StreamerBotClient()
        survived = asyncio.Event()

        async def explodes(event):
            raise ValueError("bad payload")

        async def survives(event):
            survived.set()

        client.on_event(explodes)
        client.on_event(survives)
        client._dispatch({"event": {}})

        await asyncio.wait_for(survived.wait(), 1)

    @pytest.mark.asyncio
    async def test_in_flight_tasks_are_held_onto(self):
        """asyncio keeps only a weak reference - an unheld task can be
        collected mid-await."""
        import asyncio

        client = streamerbot_client.StreamerBotClient()
        release = asyncio.Event()

        async def blocks(event):
            await release.wait()

        client.on_event(blocks)
        client._dispatch({"event": {}})

        assert len(client._listener_tasks) == 1
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert client._listener_tasks == set()


# ---------- Message length ----------
#
# YouTube caps a chat message at 200 characters and drops anything longer
# SILENTLY - no error, no reply, nothing in any log on this side. The
# !help text is 269 characters, so it arrived on Twitch (500) and
# vanished on YouTube, while shorter replies from the same code path went
# through fine.

class TestSplitMessage:
    def test_a_short_message_is_left_alone(self):
        assert streamerbot_client.split_message("hello", 200) == ["hello"]

    def test_a_message_exactly_at_the_limit_is_not_split(self):
        text = "x" * 200
        assert streamerbot_client.split_message(text, 200) == [text]

    def test_a_long_message_is_split_on_spaces(self):
        text = " ".join(["word"] * 100)  # 499 characters
        parts = streamerbot_client.split_message(text, 200)

        assert len(parts) > 1
        for part in parts:
            assert len(part) <= 200
        assert " ".join(parts) == text

    def test_words_are_never_broken_in_half(self):
        parts = streamerbot_client.split_message("alpha bravo charlie delta", 12)
        for part in parts:
            for word in part.split():
                assert word in ("alpha", "bravo", "charlie", "delta")

    def test_a_single_word_longer_than_the_limit_is_still_sent(self):
        """A URL, or a weapon list that lost its spaces, has to go somewhere."""
        parts = streamerbot_client.split_message("x" * 50, 10)
        assert parts
        assert all(len(part) <= 10 for part in parts)

    def test_it_stops_rather_than_flooding_the_channel(self):
        """Chat is not a document - a bug that makes a long string must
        not turn into the bot posting all day."""
        text = " ".join(["word"] * 1000)
        parts = streamerbot_client.split_message(text, 200)

        assert len(parts) == streamerbot_client.MAX_MESSAGE_PARTS
        assert parts[-1].endswith("…")

    def test_the_truncation_marker_fits_inside_the_limit(self):
        text = " ".join(["word"] * 1000)
        parts = streamerbot_client.split_message(text, 200)
        assert all(len(part) <= 200 for part in parts)

    def test_the_real_help_message_fits_youtube_in_two_parts(self):
        help_text = (
            "Commands: !roulette (500 points) opens a vote for next round's forced buy - "
            "vote with !<weapon> while it's open. Weapons: classic, shorty, frenzy, ghost, "
            "sheriff, stinger, spectre, bucky, judge, bulldog, guardian, phantom, vandal, "
            "marshal, outlaw, operator, ares, odin."
        )
        parts = streamerbot_client.split_message(help_text, 200)

        assert all(len(part) <= 200 for part in parts)
        assert not parts[-1].endswith("…")   # nothing lost


class TestMessageLimit:
    def test_twitch_and_youtube_differ(self):
        assert streamerbot_client.message_limit("twitch") == 500
        assert streamerbot_client.message_limit("youtube") == 200

    def test_the_platform_name_is_case_insensitive(self):
        """Streamer.bot's envelope says "Twitch" and "YouTube"."""
        assert streamerbot_client.message_limit("YouTube") == 200

    def test_an_unknown_platform_gets_the_lower_limit(self):
        """A reply split in two is cosmetic; a reply silently dropped is the bug."""
        assert streamerbot_client.message_limit("kick") == 200
        assert streamerbot_client.message_limit("") == 200


class TestSendingALongReply:
    @pytest.mark.asyncio
    async def test_a_long_reply_goes_out_as_several_messages(self):
        client = streamerbot_client.StreamerBotClient()
        sent = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        client._ws = FakeWS()
        await client.send_chat_message(" ".join(["word"] * 100), platform="youtube")

        assert len(sent) > 1
        assert all(len(p["message"]) <= 200 for p in sent)
        assert all(p["platform"] == "youtube" for p in sent)

    @pytest.mark.asyncio
    async def test_the_same_reply_is_one_message_on_twitch(self):
        """499 characters fits Twitch's 500 and not YouTube's 200."""
        client = streamerbot_client.StreamerBotClient()
        sent = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        client._ws = FakeWS()
        await client.send_chat_message(" ".join(["word"] * 100), platform="twitch")

        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_every_part_is_remembered_for_the_echo_guard(self):
        """
        Each part comes back down the subscription as its own chat event,
        and the guard matches whole messages - so remembering only the
        original would let the parts through as if a viewer had typed them.
        """
        client = streamerbot_client.StreamerBotClient()
        sent = []

        class FakeWS:
            async def send_json(self, payload):
                sent.append(payload)

        client._ws = FakeWS()
        await client.send_chat_message(" ".join(["word"] * 100), platform="youtube")

        for payload in sent:
            assert client._is_our_own_message(payload["message"]) is True
