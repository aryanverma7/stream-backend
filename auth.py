"""
GitHub OAuth for the admin dashboard, per Task #4's design (Section 14):
gated to ONE specific authorized GitHub account, chosen so we build/store
zero custom password logic ourselves - GitHub handles the credential
entirely, matching the reference repo's own pattern.

Flow:
  GET /auth/login    -> redirect to GitHub's OAuth consent screen
  GET /auth/callback -> exchange the returned code for a token, fetch the
                         GitHub username, check it against the one
                         authorized account, issue a session cookie if it
                         matches

Session storage is a simple in-memory set - appropriate for a single-admin
personal tool in one process, no need for anything heavier.
"""
import secrets

import aiohttp
from aiohttp import web

from config import config
from logger import get_logger

log = get_logger("Auth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

SESSION_COOKIE_NAME = "dashboard_session"

# In-memory session store. Fine for this scale - one admin, one process.
_valid_sessions: set[str] = set()


def _redirect_uri() -> str:
    """
    Must match EXACTLY what's registered in the GitHub OAuth App settings,
    including the scheme (https) - this needs the Cloudflare Tunnel domain
    from Task #3 to be set up first, since GitHub OAuth requires a real
    reachable HTTPS URL, not localhost.
    """
    base = config.get("public_base_url", "")
    if not base:
        log.warning("public_base_url is empty in config.json - GitHub OAuth callback will not work yet")
    return f"{base}/auth/callback"


async def handle_login(request: web.Request) -> web.Response:
    client_id = config.get("github_client_id", "")
    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "scope": "read:user",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    raise web.HTTPFound(f"{GITHUB_AUTHORIZE_URL}?{query}")


async def handle_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    if not code:
        return web.Response(status=400, text="Missing code parameter")

    async with aiohttp.ClientSession() as session:
        # Exchange the code for an access token
        async with session.post(
            GITHUB_TOKEN_URL,
            data={
                "client_id": config.get("github_client_id", ""),
                "client_secret": config.get("github_client_secret", ""),
                "code": code,
                "redirect_uri": _redirect_uri(),
            },
            headers={"Accept": "application/json"},
        ) as resp:
            token_data = await resp.json()
            access_token = token_data.get("access_token")
            if not access_token:
                log.warning(f"GitHub token exchange failed: {token_data}")
                return web.Response(status=401, text="GitHub authentication failed")

        # Fetch the authenticated user's GitHub username
        async with session.get(
            GITHUB_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            user_data = await resp.json()
            github_username = user_data.get("login", "")

    authorized_usernames = config.get("authorized_github_usernames", [])
    if not authorized_usernames:
        log.warning("authorized_github_usernames is empty in config.json - rejecting all logins")
        return web.Response(status=403, text="Dashboard not yet configured with any authorized accounts")

    authorized_lower = [u.lower() for u in authorized_usernames]
    if github_username.lower() not in authorized_lower:
        log.warning(f"Rejected login attempt from unauthorized GitHub account: {github_username}")
        return web.Response(status=403, text="This GitHub account is not authorized for this dashboard")

    session_token = secrets.token_urlsafe(32)
    _valid_sessions.add(session_token)
    log.info(f"Dashboard login successful: {github_username}")

    response = web.HTTPFound("/")
    response.set_cookie(SESSION_COOKIE_NAME, session_token, httponly=True, samesite="Lax")
    raise response


def is_authenticated(request: web.Request) -> bool:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return token is not None and token in _valid_sessions


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """
    Protects the admin dashboard's own routes (/api/config, /api/points/*,
    /api/logs, /api/status, and eventually the /admin frontend page) while
    leaving everything meant for public visitors open: the homepage itself,
    the public clips API, the actual clip video files, the widget websocket,
    the health check, and the login flow itself.

    Note: "/" is listed here for the future homepage/frontend (not yet
    built) - a public visitor should never need to log in just to see the
    landing page. /admin is deliberately NOT listed here, since that's
    where the actual protected dashboard UI will live once the frontend
    exists - anything not explicitly open defaults to requiring auth.
    """
    open_paths = (
        "/health", "/ws/widgets",
        "/auth/login", "/auth/callback",
        "/api/public", "/clips",
        "/_next",       # the site's own JS/CSS bundles
        "/favicon.ico", # sits at the out/ root, not under /_next
        "/dragon-original.js",  # same reason - a root-level public/ asset,
                                 # not caught by the /_next prefix match
        "/api/ocr/credit-report",  # gated by its OWN shared-secret check
                                    # (X-Agent-Secret) instead - the
                                    # gaming-PC agent that calls this has
                                    # no GitHub session to provide, so
                                    # this must be exempt from THIS
                                    # middleware for its own auth to ever
                                    # be reached at all.
        "/api/ocr/reset",  # same reasoning as above - also called by the agent, no GitHub session
        "/",
    )
    if request.path in open_paths or any(
        request.path.startswith(p) for p in open_paths if p != "/"
    ):
        return await handler(request)

    if not is_authenticated(request):
        # Visiting /admin itself (not an API call) without a session should
        # smoothly redirect into the login flow rather than show a bare
        # 401 - someone who just did the hidden gesture is expecting to
        # land somewhere, not read an error. API routes (/api/*) still get
        # a plain 401, since those are called by code, not a person
        # navigating a browser.
        if request.path == "/admin":
            raise web.HTTPFound("/auth/login")
        return web.Response(status=401, text="Not authenticated - visit /auth/login first")

    return await handler(request)
