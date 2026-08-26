"""
Widget hub - the local WebSocket server that "smart" widgets (Roulette, the
Forced Buy badge, Spotify now-playing) connect to as clients, per Task #3's
design (Section 3 / 18: distinguishing self-contained widgets like the
Rank/RR tracker from backend-fed ones).

This is SKELETON ONLY, per Task #3's explicit scope boundary: it can accept
connections, tag them, and broadcast/send arbitrary JSON messages. The actual
FEATURE messages (RouletteAPI.trigger(), badgeShow(), Spotify now-playing
updates, etc.) get built in their own later tasks and just call into
broadcast()/send_to() - nothing about those payloads is decided here.
"""
from aiohttp import web, WSMsgType
from logger import get_logger

log = get_logger("WidgetHub")


class WidgetHub:
    def __init__(self):
        # client_id -> (WebSocketResponse, tag)
        # "tag" lets later tasks target a broadcast at e.g. only "roulette"
        # clients without needing to know individual connection IDs.
        self._clients: dict[str, tuple[web.WebSocketResponse, str]] = {}
        self._next_id = 0

    async def handle_connection(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        self._next_id += 1
        client_id = f"widget-{self._next_id}"
        # Widgets identify themselves via a query param, e.g. ?widget=roulette
        tag = request.query.get("widget", "unknown")
        self._clients[client_id] = (ws, tag)
        log.info(f"Widget connected: {client_id} (tag={tag}). Total: {len(self._clients)}")

        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    # Widgets can report results back (e.g. Roulette reporting
                    # its winning gun once a spin lands, per Section 6e) -
                    # actual parsing/handling of these is a later task's job.
                    log.info(f"Message from {client_id} ({tag}): {msg.data}")
                elif msg.type == WSMsgType.ERROR:
                    log.warning(f"WS connection error for {client_id}: {ws.exception()}")
        finally:
            self._clients.pop(client_id, None)
            log.info(f"Widget disconnected: {client_id} (tag={tag}). Total: {len(self._clients)}")

        return ws

    async def broadcast(self, message: dict, tag: str | None = None):
        """
        Send a JSON message to all connected widgets, or only those matching
        `tag` (e.g. tag="roulette" to only reach the Roulette widget, leaving
        the badge/Spotify widgets untouched).
        """
        dead = []
        for client_id, (ws, client_tag) in self._clients.items():
            if tag is not None and client_tag != tag:
                continue
            try:
                await ws.send_json(message)
            except ConnectionResetError:
                dead.append(client_id)
        for client_id in dead:
            self._clients.pop(client_id, None)

    def connected_count(self, tag: str | None = None) -> int:
        if tag is None:
            return len(self._clients)
        return sum(1 for _, t in self._clients.values() if t == tag)


# Single shared instance - other modules import this to broadcast to widgets.
widget_hub = WidgetHub()
