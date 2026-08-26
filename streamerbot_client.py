"""
Outbound WebSocket connection to Streamer.bot (running on the gaming PC),
per Section 18's final hybrid architecture: Streamer.bot handles multi-platform
chat connections, this backend owns all the bespoke game-specific logic and
just listens for relayed chat events over this connection.

SKELETON ONLY, per Task #3's scope: this handles connecting, reconnecting
with backoff, and logging events as they arrive. Actual command handling
(!weight, !roulette, !clip, etc.) gets wired in during their own later tasks -
this module just gives them a stream of events to listen to.
"""
import asyncio
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


class StreamerBotClient:
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._attempt = 0
        self._listeners: list = []  # callables invoked with each parsed event dict
        self._running = False

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

    async def _connect_and_listen(self):
        url = config.get("streamerbot_ws_url", "ws://localhost:8080/")
        log.info(f"Connecting to Streamer.bot at {url}")
        async with self._session.ws_connect(url) as ws:
            self._ws = ws
            log.info("Connected to Streamer.bot")

            # NOTE for later tasks: this is where a Subscribe request would be
            # sent (e.g. requesting ChatMessage, Follow, Cheer events) - which
            # exact events to subscribe to depends on what each feature task
            # actually needs, so left undecided here on purpose.

            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except json.JSONDecodeError:
                        log.warning(f"Non-JSON message from Streamer.bot: {msg.data}")
                        continue
                    for callback in self._listeners:
                        await callback(event)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    log.warning(f"Streamer.bot WS error: {ws.exception()}")
                    break

        self._ws = None


# Single shared instance - feature tasks import this and call .on_event(...)
streamerbot = StreamerBotClient()


async def forward_chat_to_widgets(event: dict):
    """
    Real chat relay for the public site's merged chat display (spec
    Section 7). Filters to ChatMessage events only, forwards a clean
    platform-tagged shape to any widget connected with tag="chat" -
    which is exactly what the site's chat component will use.
    """
    if event.get("event", {}).get("type") != "ChatMessage":
        return

    data = event.get("data", {})
    platform = data.get("source", {}).get("platform", "unknown")
    message = data.get("message", {})

    await widget_hub.broadcast(
        {
            "type": "chat_message",
            "platform": platform,
            "username": message.get("username", ""),
            "message": message.get("message", ""),
        },
        tag="chat",
    )
