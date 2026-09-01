"""
Entry point for the Mac Mini backend.

Per Task #3's design: a SINGLE asyncio process running everything
concurrently (HTTP server, widget WebSocket server, Streamer.bot client
connection). No microservices, no separate processes to coordinate -
matches the actual scale of this project (one streamer, a modest audience),
and keeps operations simple: one thing to keep alive, one log stream, one
thing to restart if something goes wrong.

This file is deliberately thin - it wires the pieces together and starts
them. The actual logic for each piece lives in its own module.
"""
import asyncio
import signal

import credit_ocr
import game_events
import health_checks
import points_cloudbot
import roulette
import spotify
from config import config
from logger import get_logger
from roulette import handle_chat_command as handle_roulette_command
from server import run_server
from streamerbot_client import forward_chat_to_widgets, parse_chat_message, streamerbot
from streamlabs_socket import start_tips_listener, stop_tips_listener

log = get_logger("Main")


async def _forward_chat_to_cloudbot_points(event: dict):
    """
    Hands parsed chat to the Cloudbot points backend, which is waiting on
    replies to the !points/!addpoints/!removepoints it sends. Reads the
    event through parse_chat_message() for the same reason every other
    listener does - so no two of them can disagree about the payload shape.
    """
    chat = parse_chat_message(event)
    if chat is not None:
        await points_cloudbot.handle_chat_event(chat)


async def main():
    log.info("Mac Mini backend starting up")

    # Widget-facing HTTP + WebSocket server
    runner = await run_server(
        host=config.get("http_host", "0.0.0.0"),
        port=config.get("http_port", 8765),
    )

    # Periodic check that the public hostname still reaches this process.
    # Started after the server is listening, since the very first probe
    # goes out to Cloudflare and comes straight back into the route above.
    await health_checks.start()

    # Outbound connection to Streamer.bot (Section 18's hybrid architecture)
    streamerbot.on_event(forward_chat_to_widgets)
    streamerbot.on_event(handle_roulette_command)

    # Cloudbot answers in chat, so the points ledger that talks to it has
    # to read chat too. Without this listener every spend times out
    # waiting for a confirmation nothing is reading.
    streamerbot.on_event(_forward_chat_to_cloudbot_points)

    # Song requests. Its own listener rather than another branch inside
    # the roulette's dispatcher: they are unrelated features that happen
    # to read the same stream, and one of them being switched off in
    # config should not mean walking past the other's code to find out.
    streamerbot.on_event(spotify.handle_chat_command)

    # The gaming PC's /api/ocr/reset is the only real "a new round has
    # begun" signal here, and the forced-buy badge needs it as much as the
    # OCR reading window does - without it the badge only knows how much
    # time has passed, which is a guess about rounds, not a reading of one.
    credit_ocr.on_new_buy_phase(roulette.on_new_buy_phase)

    # The same signal from a source that can actually see the game, rather
    # than inferring it from a keypress. Registered ALONGSIDE the OCR one
    # rather than instead of it, because the two fail differently: the OCR
    # signal needs the streamer to press B, so a round where they never
    # open the menu produces nothing, while this one needs Overwolf to be
    # running and unbroken by today's patch. With both live every phase is
    # reported twice, which roulette.NEW_BUY_PHASE_DEBOUNCE_SECONDS exists
    # to collapse back into one.
    game_events.on_buy_phase(roulette.on_new_buy_phase)
    await streamerbot.start()

    # Streamlabs tips -> points (Task #6) - genuinely optional at startup:
    # a fresh install has no streamlabs_access_token yet (that only exists
    # once the streamer completes the OAuth flow from the admin dashboard),
    # and the rest of the backend - the site, the admin dashboard, chat -
    # must keep working regardless of whether this specific integration is
    # connected yet.
    # Off by default, full stop. Granting here means posting !addpoints
    # into chat, and Cloudbot already pays out on donations natively - so
    # a donor would be granted twice, and every tip would leave a visible
    # bot line in chat as well.
    #
    # Turn it on with streamlabs_tips_listener_enabled only after turning
    # Cloudbot's own donation payout off. One of the two has to do it, and
    # never both.
    if not config.get("streamlabs_tips_listener_enabled", False):
        log.info("Streamlabs tips listener is disabled - donations are expected to pay out elsewhere")
    elif config.get("streamlabs_access_token", ""):
        try:
            await start_tips_listener()
        except Exception:
            log.exception("Could not connect to Streamlabs Socket API - tips won't grant points until this is fixed")
    else:
        log.info("No streamlabs_access_token configured yet - skipping tips listener (connect via /auth/streamlabs/login)")

    log.info("Backend is up. Waiting for events / connections.")

    # Keep the process alive until interrupted (Ctrl+C locally, or a real
    # stop signal when running under launchd).
    stop_event = asyncio.Event()

    def _handle_stop(*_):
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_stop)

    await stop_event.wait()

    log.info("Shutting down")
    await stop_tips_listener()
    await health_checks.stop()
    await streamerbot.stop()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
