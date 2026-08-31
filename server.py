"""
The HTTP server. Hosts:
  - GET /health              - basic liveness check
  - GET /ws/widgets          - WebSocket upgrade route for smart widgets
                                (Roulette, badge, Spotify, and the public
                                site's live chat display)
  - GET /auth/login,
    GET /auth/callback       - GitHub OAuth login flow for the dashboard
  - /api/public/*, /clips/*  - PUBLIC routes (clips listing + video files),
                                open to any visitor, no login needed
  - /api/*                   - admin dashboard API (Task #4), protected by
                                auth_middleware (config, points, logs, status)
  - / and /admin             - the built showcase site (Next.js static
                                export), served directly from wherever the
                                sibling frontend project's out/ folder
                                actually lives (see site_server.py) -
                                registered LAST so it never shadows any of
                                the more specific routes above
"""
from aiohttp import web

import auth
import credit_ocr
import dashboard_api
import game_events
import health_checks
import public_api
import site_server
import streamlabs_oauth
from logger import get_logger
from widget_hub import widget_hub

log = get_logger("HTTP")


async def handle_health(request: web.Request) -> web.Response:
    """
    Liveness check. The instance token is what lets health_checks.py tell
    "the public URL reaches THIS backend" apart from "something answers on
    that hostname" - a stale tunnel or a misdirected DNS record answers
    the first half of this response just as happily as the real thing.
    """
    return web.json_response({"status": "ok", "instance": health_checks.INSTANCE_ID})


async def handle_widget_ws(request: web.Request) -> web.WebSocketResponse:
    return await widget_hub.handle_connection(request)


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth.auth_middleware])
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ws/widgets", handle_widget_ws)
    app.router.add_get("/auth/login", auth.handle_login)
    app.router.add_get("/auth/callback", auth.handle_callback)
    # Streamlabs OAuth (Task #5) - deliberately NOT added to auth.py's
    # open_paths. Only an already-authenticated admin (via the GitHub OAuth
    # session above) should ever be able to trigger a Streamlabs connection
    # attempt, not any random visitor to the site.
    app.router.add_get("/auth/streamlabs/login", streamlabs_oauth.streamlabs_login)
    app.router.add_get("/auth/streamlabs/callback", streamlabs_oauth.streamlabs_callback)
    # Task #8's credit OCR reporting - IS in auth.py's open_paths (exempt
    # from the GitHub OAuth admin check), gated instead by its own
    # shared-secret check inside the handler - the gaming-PC agent calling
    # this has no GitHub session to provide.
    app.router.add_post("/api/ocr/credit-report", credit_ocr.handle_credit_report)
    app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
    app.router.add_post("/api/ocr/heartbeat", credit_ocr.handle_heartbeat)
    # Live game state from the Overwolf app on the gaming PC - same
    # machine, same shared secret, same open_paths exemption as the three
    # OCR routes above.
    game_events.register_routes(app)
    public_api.register_public_routes(app)
    dashboard_api.register_routes(app)

    # Registered LAST, deliberately - confirmed by testing that aiohttp
    # resolves the more specific routes above before falling through to
    # this site-serving catch-all for everything else (_next assets, etc.)
    try:
        site_server.register_site_routes(app)
    except RuntimeError as e:
        log.warning(f"Site serving disabled: {e}")

    return app


async def run_server(host: str, port: int):
    app = build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info(f"HTTP/WebSocket server listening on http://{host}:{port}")
    return runner
