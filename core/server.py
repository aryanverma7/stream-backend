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

from core import auth
from stream import break_timer
from stream import credit_ocr
from core import dashboard_api
from editor import editor_api
from core import health_checks
from core import public_api
from core import site_server
from stream import spotify_oauth
from editor import gdrive_oauth
from stream import streamlabs_oauth
from editor import uploader_api
from core.logger import get_logger
from stream.widget_hub import widget_hub

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


# 'unsafe-inline' scripts: Next's static export inlines its bootstrap, and a nonce needs a server.
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' https://www.googleapis.com; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


async def _security_headers(request: web.Request, response: web.StreamResponse) -> None:
    """
    A response-prepare hook, not a middleware: a FileResponse only knows its
    content type once it is being sent, and auth's 401/302s and raised
    HTTPNotFounds get headed too. The homepage keeps the frame ban only -
    it loads cdnjs, S3, Twitch and YouTube content a strict policy would block.
    """
    h = response.headers
    path = request.path
    # Every page but the homepage, so /admin.html or any other spelling cannot dodge it.
    if response.content_type == "text/html" and path not in ("/", "/index.html"):
        h.setdefault("Content-Security-Policy", PAGE_CSP)
    h.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    h.setdefault("X-Content-Type-Options", "nosniff")


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth.auth_middleware])
    app.on_response_prepare.append(_security_headers)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ws/widgets", handle_widget_ws)
    app.router.add_get("/auth/login", auth.handle_login)
    app.router.add_get("/auth/callback", auth.handle_callback)
    # NOT in open_paths: logging out needs a session to log out of.
    app.router.add_post("/auth/logout", auth.handle_logout)
    # Streamlabs OAuth (Task #5) - deliberately NOT added to auth.py's
    # open_paths. Only an already-authenticated admin (via the GitHub OAuth
    # session above) should ever be able to trigger a Streamlabs connection
    # attempt, not any random visitor to the site.
    app.router.add_get("/auth/streamlabs/login", streamlabs_oauth.streamlabs_login)
    app.router.add_get("/auth/streamlabs/callback", streamlabs_oauth.streamlabs_callback)
    # Spotify song requests (Task #12) - same admin-only gating and the
    # same reason for it.
    app.router.add_get("/auth/spotify/login", spotify_oauth.spotify_login)
    app.router.add_get("/auth/spotify/callback", spotify_oauth.spotify_callback)
    app.router.add_get("/auth/gdrive/login", gdrive_oauth.gdrive_login)
    app.router.add_get("/auth/gdrive/callback", gdrive_oauth.gdrive_callback)
    # Task #8's credit OCR reporting - IS in auth.py's open_paths (exempt
    # from the GitHub OAuth admin check), gated instead by its own
    # shared-secret check inside the handler - the gaming-PC agent calling
    # this has no GitHub session to provide.
    app.router.add_post("/api/ocr/credit-report", credit_ocr.handle_credit_report)
    app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
    app.router.add_post("/api/ocr/heartbeat", credit_ocr.handle_heartbeat)
    # The break screen's countdown. Admin-gated like the rest of /api/ -
    # the overlay is told over the widget socket, never by calling this.
    break_timer.register_routes(app)
    public_api.register_public_routes(app)
    dashboard_api.register_routes(app)
    # The editor app. Both roles reach /api/editor/* through auth.py's
    # EDITOR_PATHS; streamer-only actions are refused inside the handlers.
    editor_api.register_routes(app)
    # The gaming-PC VOD uploader: no session, X-Agent-Secret checked in the handlers.
    uploader_api.register_routes(app)

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
