"""
Outbound WebSocket connection to Streamer.bot (running on the gaming PC),
per Section 18's final hybrid architecture: Streamer.bot handles multi-platform
chat connections, this backend owns all the bespoke game-specific logic and
just listens for relayed chat events over this connection.

Started life as a connect/reconnect skeleton; the pieces that make it
actually carry traffic are now here too:

  * A Subscribe request is sent as soon as the socket opens. This is not
    optional and it is the whole reason nothing worked before it existed -
    Streamer.bot's own documentation is explicit that "Events are not sent
    unless a subscription is requested." The socket connects, the admin
    dashboard reports Streamer.bot as Connected, and not one chat message
    ever arrives. `is_subscribed` is tracked separately from
    `is_connected` for exactly that reason, so the dashboard can tell the
    two apart instead of showing a reassuring green light over a silent
    connection.

  * Request RESPONSES are separated from EVENTS before the listener
    fan-out. A response carries `status` and the `id` we sent; an event
    carries an `event` object. Handing a response to listeners written to
    expect events is harmless but useless, and it would hide a failed
    subscription instead of logging it.

  * `send_chat_message()` gives feature modules a way to answer a viewer
    in chat, via the SendMessage request.

  * Streamer.bot's own challenge-response authentication is implemented,
    and is the expected way to run this. SendMessage is the one request
    Streamer.bot marks "Authentication Required", so chat replies need it;
    turning the server's Enforce option on additionally requires it before
    Subscribe, which is what keeps anything else on the network from
    reading chat through this port. The handshake is the same shape
    obs-websocket uses: the server's Hello carries a `salt` and a
    `challenge`, and the answer is
    base64(sha256(base64(sha256(password + salt)) + challenge)).

    Because of Enforce, the ordering here is load-bearing: Hello, then
    Authenticate, then Subscribe. Subscribing straight after connect - what
    this did before - is rejected outright on an enforcing server.
"""
import asyncio
import base64
import hashlib
import itertools
import json
import time
from collections import deque

import aiohttp

from config import config
from logger import get_logger
from widget_hub import widget_hub

log = get_logger("StreamerBot")

# Backoff pattern, same shape as the one already proven out in the Rank/RR
# tracker widget's own OBS WebSocket reconnect logic - consistent handling
# of "primary connection that should self-heal" across this whole project.
RETRY_DELAYS = [1, 5, 10, 20, 30]  # seconds

# What to ask Streamer.bot to send us. Overridable via config.json's
# "streamerbot_subscribe_events" using Streamer.bot's own category/name
# spelling, so a new feature that needs Cheer or Follow events is a config
# edit rather than a code change. The two defaults here are the chat
# events the roulette runs on: Twitch spells its chat event "ChatMessage",
# YouTube spells its own "Message".
DEFAULT_SUBSCRIPTIONS = {
    "Twitch": ["ChatMessage"],
    "YouTube": ["Message"],
}

# Event type names that mean "somebody said something in chat", across the
# platforms above.
CHAT_EVENT_TYPES = {"ChatMessage", "Message"}

_request_ids = itertools.count(1)

# How long a message this backend sent stays recognizable when it comes
# back as a chat event. Only needs to outlast the round trip out to
# Streamer.bot, into Twitch, and back down the subscription.
SENT_MESSAGE_TTL_SECONDS = 30


def _authentication_hash(password: str, salt: str, challenge: str) -> str:
    """
    Streamer.bot's challenge-response answer, computed exactly as its
    documentation specifies: the password is salted and hashed first, and
    that intermediate result - as its base64 text, not as raw bytes - is
    then hashed again against the per-connection challenge.

    The two-step shape is the point: the salted half is stable per password
    and could in principle be stored, while the challenge changes every
    connection, so a captured answer cannot be replayed onto a later one.
    """
    salted = base64.b64encode(
        hashlib.sha256((password + salt).encode("utf-8")).digest()
    ).decode("utf-8")
    return base64.b64encode(
        hashlib.sha256((salted + challenge).encode("utf-8")).digest()
    ).decode("utf-8")


def _next_request_id(prefix: str) -> str:
    return f"{prefix}-{next(_request_ids)}"


def _first_string(source: dict, *keys: str) -> str:
    """First non-empty string among `keys`, or "" - used to read the same
    logical field out of payload shapes that spell it differently."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def parse_chat_message(event: dict) -> "dict | None":
    """
    Normalizes a Streamer.bot chat event into
    {"platform", "username", "display_name", "text"}, or None if this
    isn't a chat event at all.

    Deliberately accepts more than one payload shape. Current Streamer.bot
    builds put the chatter in `data.user` (`login`/`name`) and the text in
    `data.text`; this backend was originally written against an older
    shape with `data.message.username` and `data.message.message`, and
    YouTube's Message event has no published schema at all. Rather than
    bet the whole roulette on which build is installed on the gaming PC,
    read whichever of them is actually present. A wrong guess here does
    not fail loudly - it just means no command ever fires, which is
    indistinguishable from "nobody used the command."
    """
    envelope = event.get("event", {})
    if envelope.get("type") not in CHAT_EVENT_TYPES:
        return None

    data = event.get("data", {})
    if not isinstance(data, dict):
        return None

    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    legacy = data.get("message") if isinstance(data.get("message"), dict) else {}

    username = _first_string(user, "login", "name", "displayName") or _first_string(
        legacy, "username", "displayName"
    )
    display_name = (
        _first_string(user, "name", "displayName")
        or _first_string(legacy, "displayName", "username")
        or username
    )

    text = _first_string(data, "text")
    if not text and isinstance(data.get("message"), str):
        text = data["message"]
    if not text:
        text = _first_string(legacy, "message", "text")

    # The platform lives on the envelope (`event.source`) in every current
    # build. The nested lookup is the older shape this module first read.
    platform = envelope.get("source") or (
        data.get("source", {}).get("platform") if isinstance(data.get("source"), dict) else None
    )

    return {
        "platform": str(platform).lower() if platform else "unknown",
        "username": username,
        "display_name": display_name,
        "text": text,
    }


class StreamerBotClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._attempt = 0
        self._listeners: list = []  # callables invoked with each parsed event dict
        self._running = False
        self._subscribed = False
        # Text of every message this backend has sent recently, so its own
        # replies can be recognized when Streamer.bot relays them straight
        # back as chat events. See _is_our_own_message().
        self._recently_sent: deque = deque()
        # Tri-state on purpose. None means the server never asked us to
        # authenticate (its Authentication toggle is off), which is a
        # different situation from an attempt that failed - and only the
        # failure is worth warning about.
        self._authenticated: "bool | None" = None

    def on_event(self, callback):
        """
        Register a callback for incoming Streamer.bot events. Later tasks
        (Roulette, Spotify, YouTube !clip relay) call this to subscribe
        without needing to touch this module's connection logic at all.
        """
        self._listeners.append(callback)

    @property
    def is_connected(self) -> bool:
        """Used by the admin dashboard's status panel (Task #4)."""
        return self._ws is not None

    @property
    def is_subscribed(self) -> bool:
        """
        Whether Streamer.bot has acknowledged our Subscribe request. Kept
        separate from `is_connected` on purpose: an open socket with no
        accepted subscription delivers nothing at all, which looks exactly
        like a quiet chat. The dashboard shows both so that state is
        visible rather than guessed at.
        """
        return self._subscribed

    @property
    def is_authenticated(self) -> "bool | None":
        """
        None when the server's Authentication toggle is off and it never
        issued a challenge; True/False once one was answered. False is the
        interesting case: chat replies will be rejected, because
        SendMessage is the request Streamer.bot marks as requiring
        authentication.
        """
        return self._authenticated

    async def start(self):
        self._running = True
        self._session = aiohttp.ClientSession()
        asyncio.create_task(self._connection_loop())

    async def stop(self):
        self._running = False
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()

    async def _connection_loop(self):
        while self._running:
            try:
                await self._connect_and_listen()
                self._attempt = 0  # clean disconnect, reset backoff
            except Exception as e:
                log.warning(f"Connection lost/failed: {e}")

            if not self._running:
                break

            delay = RETRY_DELAYS[min(self._attempt, len(RETRY_DELAYS) - 1)]
            self._attempt += 1
            log.info(f"Reconnecting to Streamer.bot in {delay}s (attempt {self._attempt})")
            await asyncio.sleep(delay)

    async def send_chat_message(self, message: str, platform: str = "twitch") -> bool:
        """
        Sends a chat message back out through Streamer.bot. Returns whether
        the request was handed to the socket at all - not whether chat
        actually accepted it, which only arrives later as a response and is
        logged by _handle_response().

        Returns False rather than raising when there's no connection: a
        viewer not getting an answer in chat is a worse outcome than a
        crash only in the sense that it's quieter, and every caller here is
        a chat-command handler that has already done the real work.
        """
        ws = self._ws
        if ws is None:
            log.warning(f"Not connected to Streamer.bot - dropping chat message: {message!r}")
            return False

        # Whether to speak as Streamer.bot's configured BOT account or as
        # the broadcaster's own. This is a per-setup fact, not a constant:
        # `bot: true` requires a second account to be connected in
        # Streamer.bot, and if none is, the request is accepted and
        # nothing is ever posted - which from here is indistinguishable
        # from a working reply nobody happened to read. Overridable so
        # that is a config edit rather than a deploy.
        as_bot = config.get("streamerbot_send_as_bot", True)

        await ws.send_json({
            "request": "SendMessage",
            "id": _next_request_id("send"),
            "platform": platform,
            "bot": as_bot,
            "internal": False,
            "message": message,
        })
        # Logged on the way out, not just on failure. Before this, a
        # successful send produced no log line anywhere, so "the bot
        # replied and chat didn't show it" and "the bot never tried"
        # looked identical in the log - which is exactly the question
        # being asked whenever a chat reply goes missing.
        log.info(f"Sent chat message to {platform} (bot={as_bot}): {message!r}")
        self._recently_sent.append((time.monotonic(), message))
        return True

    def _is_our_own_message(self, text: str) -> bool:
        """
        Whether this exact text is something this backend just said.

        Chat replies come straight back down the subscription as ordinary
        chat events, and a reply that happens to be command-shaped is then
        obeyed. The !help reply opens with "!roulette", so answering !help
        parsed as a !roulette trigger and charged the asker for a session
        they never asked for - a feedback loop that only stayed harmless
        because the account it fired on was too poor to afford it.

        Matched on the exact text rather than on the sender, because with
        streamerbot_send_as_bot off the sender IS the broadcaster, and the
        streamer typing a real command in their own chat has to keep
        working. No reply this backend sends is a plausible thing for a
        viewer to type verbatim inside the TTL.
        """
        now = time.monotonic()
        while self._recently_sent and now - self._recently_sent[0][0] > SENT_MESSAGE_TTL_SECONDS:
            self._recently_sent.popleft()
        return any(sent == text for _, sent in self._recently_sent)

    async def _handle_hello(self, ws, payload: dict) -> None:
        """
        Streamer.bot opens every connection with a Hello. When its
        Authentication toggle is on, that Hello carries the salt and
        challenge for this connection, and Subscribe must wait until the
        Authenticate request has been answered - with the Enforce option
        on, an unauthenticated Subscribe is rejected outright.
        """
        auth = payload.get("authentication")
        if not isinstance(auth, dict):
            self._authenticated = None
            await self._subscribe(ws)
            return

        password = config.get("streamerbot_ws_password", "")
        if not password:
            self._authenticated = False
            log.error(
                "Streamer.bot asked us to authenticate but streamerbot_ws_password is empty in "
                "config.json. Chat replies will be rejected, and if the server's Enforce option "
                "is on, no events will arrive either."
            )
            # Subscribing anyway: with Enforce off this still gets us chat,
            # which is most of the value. If it's on, the rejection is
            # logged by _handle_response and both problems are visible.
            await self._subscribe(ws)
            return

        answer = _authentication_hash(password, auth.get("salt", ""), auth.get("challenge", ""))
        log.info("Answering Streamer.bot's authentication challenge")
        await ws.send_json({
            "request": "Authenticate",
            "id": _next_request_id("authenticate"),
            "authentication": answer,
        })

    async def _subscribe(self, ws) -> None:
        events = config.get("streamerbot_subscribe_events", DEFAULT_SUBSCRIPTIONS)
        request_id = _next_request_id("subscribe")
        log.info(f"Subscribing to Streamer.bot events: {events}")
        await ws.send_json({"request": "Subscribe", "id": request_id, "events": events})

    async def _handle_response(self, ws, payload: dict) -> None:
        """
        Handles a request response (as opposed to an event). Two of them
        change behaviour - Authenticate gates Subscribe, and Subscribe
        gates every event - and everything else is logged so a rejected
        SendMessage is visible instead of silent.
        """
        status = payload.get("status")
        request_id = str(payload.get("id", ""))

        if request_id.startswith("authenticate"):
            if status == "ok":
                self._authenticated = True
                log.info("Authenticated with Streamer.bot")
            else:
                self._authenticated = False
                log.error(
                    f"Streamer.bot REJECTED authentication ({payload}). Check that "
                    f"streamerbot_ws_password matches the WebSocket server's password."
                )
            # Subscribe either way: a wrong password with the server's
            # Enforce option off still leaves chat readable, and the
            # subscription's own response says whether that held.
            await self._subscribe(ws)
            return

        if request_id.startswith("subscribe"):
            if status == "ok":
                self._subscribed = True
                log.info(f"Streamer.bot accepted the event subscription: {payload.get('events', {})}")
            else:
                self._subscribed = False
                log.error(
                    f"Streamer.bot REJECTED the event subscription ({payload}). No chat events will "
                    f"arrive, so no chat command can fire - check the WebSocket server's settings."
                )
            return

        if status != "ok":
            log.warning(f"Streamer.bot rejected request {request_id or '<no id>'}: {payload}")
        else:
            # Accepted responses are logged too, which for SendMessage is
            # the difference between three outcomes that were previously
            # one: Streamer.bot took the message and something downstream
            # dropped it; Streamer.bot refused it; or Streamer.bot never
            # answered at all, which is what an unrecognized request shape
            # looks like. Without this line all three read as silence.
            log.info(f"Streamer.bot accepted request {request_id or '<no id>'}: {payload}")

    async def _connect_and_listen(self):
        url = config.get("streamerbot_ws_url", "ws://localhost:8080/")
        log.info(f"Connecting to Streamer.bot at {url}")
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            log.info("Connected to Streamer.bot")

            # No Subscribe here. Streamer.bot sends nothing until asked, but
            # the asking has to wait for its Hello: an enforcing server
            # rejects a Subscribe that arrives before Authenticate. The
            # whole handshake runs per connection rather than once at
            # startup, because both the session and the subscription belong
            # to the socket - a reconnect after the gaming PC sleeps has to
            # do it all again.

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning(f"Non-JSON message from Streamer.bot: {msg.data}")
                        continue

                    if payload.get("request") == "Hello":
                        await self._handle_hello(ws, payload)
                        continue

                    if "event" not in payload and "status" in payload:
                        await self._handle_response(ws, payload)
                        continue

                    # Dropped here rather than in each listener, so the
                    # guard cannot be present in one consumer and missing
                    # from the next one somebody adds.
                    chat = parse_chat_message(payload)
                    if chat is not None and self._is_our_own_message(chat["text"]):
                        log.info(f"Ignoring our own chat message echoed back: {chat['text']!r}")
                        continue

                    for callback in self._listeners:
                        await callback(payload)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning(f"Streamer.bot WS error: {ws.exception()}")
                    break

        self._ws = None
        self._subscribed = False
        self._authenticated = None


# Single shared instance - feature tasks import this and call .on_event(...)
streamerbot = StreamerBotClient()


async def forward_chat_to_widgets(event: dict):
    """
    Real chat relay for the public site's merged chat display (spec
    Section 7). Filters to chat events only, forwards a clean
    platform-tagged shape to any widget connected with tag="chat" -
    which is exactly what the site's chat component will use.

    Reads the event through parse_chat_message() rather than picking
    fields out itself, so this and the roulette's command handler can
    never end up disagreeing about where the username lives.
    """
    chat = parse_chat_message(event)
    if chat is None:
        return

    await widget_hub.broadcast(
        {
            "type": "chat_message",
            "platform": chat["platform"],
            "username": chat["username"],
            "message": chat["text"],
        },
        tag="chat",
    )
