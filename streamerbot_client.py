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
    in chat, via the SendMessage request. Note Streamer.bot marks
    SendMessage as requiring authentication on its WebSocket server; if
    that is enabled and this backend has no credentials, the request comes
    back non-ok and gets logged rather than failing silently.
"""
import asyncio
import itertools
import json

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

        await ws.send_json({
            "request": "SendMessage",
            "id": _next_request_id("send"),
            "platform": platform,
            "bot": True,
            "internal": False,
            "message": message,
        })
        return True

    async def _subscribe(self, ws) -> None:
        events = config.get("streamerbot_subscribe_events", DEFAULT_SUBSCRIPTIONS)
        request_id = _next_request_id("subscribe")
        log.info(f"Subscribing to Streamer.bot events: {events}")
        await ws.send_json({"request": "Subscribe", "id": request_id, "events": events})

    def _handle_response(self, payload: dict) -> None:
        """
        Handles a request response (as opposed to an event). The only one
        whose outcome changes behaviour is Subscribe - everything else is
        logged so a rejected SendMessage is visible instead of silent.
        """
        status = payload.get("status")
        request_id = str(payload.get("id", ""))

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

    async def _connect_and_listen(self):
        url = config.get("streamerbot_ws_url", "ws://localhost:8080/")
        log.info(f"Connecting to Streamer.bot at {url}")
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            log.info("Connected to Streamer.bot")

            # Streamer.bot sends nothing until asked. Subscribing here, on
            # every connect rather than once at startup, is deliberate: a
            # subscription belongs to the socket, so a reconnect after the
            # gaming PC sleeps or Streamer.bot restarts has to ask again.
            await self._subscribe(ws)

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning(f"Non-JSON message from Streamer.bot: {msg.data}")
                        continue

                    if "event" not in payload and "status" in payload:
                        self._handle_response(payload)
                        continue

                    for callback in self._listeners:
                        await callback(payload)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning(f"Streamer.bot WS error: {ws.exception()}")
                    break

        self._ws = None
        self._subscribed = False


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
