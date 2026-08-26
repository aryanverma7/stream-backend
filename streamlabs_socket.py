"""
Streamlabs Socket API listener (Task #6) - real-time donations -> points,
via the exact same points.grant_points() function already used and tested
by the admin dashboard's Points tool, not a separate simulation of it.

CRITICAL, verified-not-assumed compatibility detail: Streamlabs' own
documented example uses socket.io-client 2.0.3 (a Socket.IO v2 client).
Per python-socketio's own version compatibility chart, that requires
python-socketio 4.x (paired with python-engineio 3.x) - the modern 5.x
release targets Engine.IO protocol revision 4, which Streamlabs' server
does not speak, and several real-world GitHub issues describe this exact
mismatch as a silent "unsupported protocol version" connection failure.
requirements.txt pins python-socketio==4.6.1 specifically for this reason.

This is a deliberate, documented exception to this project's "stay
lightweight, prefer aiohttp alone" principle (see requirements.txt) -
Socket.IO is its own protocol on top of WebSocket (handshake framing,
ping/pong keepalive, packet types), and hand-rolling that correctly is
real protocol-level risk for a feature where a silent failure means
donations quietly stop turning into points during a live stream.

Donation event shape, confirmed directly against dev.streamlabs.com's
Socket API docs:
    {
      "type": "donation",
      "message": [
        {"name": "test", "amount": "13.37", "currency": "USD", ...}
      ]
    }
A donation event has NO "for" key (that's only present on platform-linked
events like Twitch follows/subs) - the `!eventData.for` check in
Streamlabs' own JS example is mirrored below as `"for" not in event`.
"""
import socketio
import aiohttp

from config import config
from logger import get_logger
from points import grant_points

log = get_logger("StreamlabsSocket")

SOCKET_TOKEN_URL = "https://streamlabs.com/api/v2.0/socket/token"
SOCKET_SERVER_URL = "https://sockets.streamlabs.com"

_client = None


async def fetch_socket_token() -> str:
    """GET /socket/token, per the socket.token scope - requires a valid access_token."""
    token = config.get("streamlabs_access_token", "")
    headers = {"Authorization": f"Bearer {token}", "X-Requested-With": "XMLHttpRequest"}
    async with aiohttp.ClientSession() as session:
        async with session.get(SOCKET_TOKEN_URL, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["socket_token"]


def donation_to_points(amount_str: str) -> int:
    """
    Converts a donation amount to a points grant, using the existing
    points_exchange_rate_per_inr config key (already a placeholder in
    config.example.json) - a flat rate, NOT real currency conversion
    (e.g. USD->INR), which would need a separate exchange-rate API and is
    out of scope here. The streamer sets this rate via the admin Config
    Editor already built (Task #4).
    """
    rate = config.get("points_exchange_rate_per_inr", 10)
    return round(float(amount_str) * rate)


async def handle_socket_event(event: dict) -> None:
    """
    The actual business logic for one incoming Socket API event - kept
    separate from the connection/handshake code below specifically so it's
    directly testable with a realistic sample payload, without needing a
    real Streamlabs connection.
    """
    if "for" in event:
        return  # platform-linked event (follow/sub/etc), not a donation
    if event.get("type") != "donation":
        return

    for donation in event.get("message", []):
        name = donation.get("name", "")
        amount = donation.get("amount", "0")
        if not name:
            log.warning(f"Donation event missing a donor name, skipping: {donation}")
            continue

        points = donation_to_points(amount)
        try:
            new_balance = await grant_points(name, points)
            log.info(f"Granted {points} points to {name} for a {amount} donation - new balance {new_balance}")
        except Exception:
            log.exception(f"Failed to grant points to {name} for a {amount} donation")


async def start_tips_listener():
    """
    Connects to the Socket API and registers the donation handler. Returns
    the client so callers (main.py's startup) can hold a reference for
    clean shutdown.
    """
    global _client
    socket_token = await fetch_socket_token()

    sio = socketio.AsyncClient()

    @sio.on("event")
    async def on_event(data):
        # Per Streamlabs' own docs: "the returned value will always be an
        # array type" - data arrives wrapped in a list even for a single event.
        events = data if isinstance(data, list) else [data]
        for event in events:
            await handle_socket_event(event)

    await sio.connect(f"{SOCKET_SERVER_URL}?token={socket_token}", transports=["websocket"])
    log.info("Connected to Streamlabs Socket API - listening for donations")

    _client = sio
    return sio


async def stop_tips_listener() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None
