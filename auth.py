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
                         matches, and send the browser back to whatever
                         path started the login

The `state` parameter carries that path across the round trip and doubles
as the OAuth CSRF check. Before it existed the callback always redirected
to "/", which meant the hidden dragon gesture opened the login flow and
then dropped the person back on the homepage with a valid session but no
dashboard - so the gesture had to be repeated to get anywhere.

Sessions live in the `sessions` table of the app database (db.py), so a
power cut on the Mac Mini does not log anyone out. Each row expires
SESSION_MAX_AGE_SECONDS after login and is deleted when found expired or on
POST /auth/logout. Pending logins stay in memory: one in flight across a
restart is already handled by "start over".
"""
import secrets
import time
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

import db
from config import config
from logger import get_logger

log = get_logger("Auth")

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USER_URL = "https://api.github.com/user"

SESSION_COOKIE_NAME = "dashboard_session"

SESSION_MAX_AGE_SECONDS = 2592000  # 30 days

_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','editor')),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
"""


def create_session(username: str, role: str, now: float | None = None) -> str:
    now = time.time() if now is None else now
    db.ensure(_SESSIONS_DDL)
    token = secrets.token_urlsafe(32)
    db.get().execute(
        "INSERT INTO sessions (token, username, role, created_at, expires_at) VALUES (?,?,?,?,?)",
        (token, username, role, now, now + SESSION_MAX_AGE_SECONDS),
    )
    return token


def session_for(request: web.Request, now: float | None = None) -> dict | None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    now = time.time() if now is None else now
    db.ensure(_SESSIONS_DDL)
    row = db.get().execute(
        "SELECT username, role, expires_at FROM sessions WHERE token = ?", (token,)
    ).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= now:
        db.get().execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    # Roles are re-derived from config on every request, so removing an
    # account revokes its live sessions and a demotion/promotion applies at once.
    role = role_for_username(row["username"])
    if role is None:
        db.get().execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return {"username": row["username"], "role": role}

# Logins in flight, keyed by the random `state` value handed to GitHub and
# mapping to the path the person was originally trying to reach. This does
# two jobs at once: it carries the destination across the round trip to
# GitHub (which otherwise loses it entirely), and it is the OAuth `state`
# CSRF check - a callback whose state we did not issue is rejected.
_pending_logins: dict[str, str] = {}

# A login that never completes leaves its entry behind, so the dict is
# capped rather than left to grow for the process's whole lifetime.
_MAX_PENDING_LOGINS = 32

DEFAULT_POST_LOGIN_PATH = "/admin"


def _safe_next_path(candidate: str) -> str:
    """
    Only ever returns a path on this site. GitHub sends the browser wherever
    the callback says, so an attacker-supplied ?next= would otherwise be an
    open redirect: "//evil.example" and "https://evil.example" are both
    valid targets for a Location header, and the second slash in "//" is
    what makes the first one protocol-relative rather than local.
    """
    if not candidate.startswith("/") or candidate.startswith("//"):
        return DEFAULT_POST_LOGIN_PATH
    return candidate


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

    next_path = _safe_next_path(request.query.get("next", DEFAULT_POST_LOGIN_PATH))
    state = secrets.token_urlsafe(24)
    if len(_pending_logins) >= _MAX_PENDING_LOGINS:
        _pending_logins.pop(next(iter(_pending_logins)))
    _pending_logins[state] = next_path

    params = {
        "client_id": client_id,
        "redirect_uri": _redirect_uri(),
        "scope": "read:user",
        "state": state,
    }
    # urlencode, not manual joining: redirect_uri and next_path both contain
    # characters ("/", ":") that have to be escaped to survive the round trip.
    raise web.HTTPFound(f"{GITHUB_AUTHORIZE_URL}?{urlencode(params)}")


async def handle_callback(request: web.Request) -> web.Response:
    code = request.query.get("code")
    if not code:
        return web.Response(status=400, text="Missing code parameter")

    state = request.query.get("state", "")
    if state not in _pending_logins:
        # Either a genuine CSRF attempt, or a stale callback: the pending
        # logins live in memory, so a backend restart mid-login lands here
        # too. Both cases want the same answer - start over.
        log.warning("Rejected an OAuth callback whose state we did not issue")
        return web.Response(status=400, text="Login session expired - visit /auth/login again")
    next_path = _pending_logins.pop(state)

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

    role = role_for_username(github_username)
    if role is None:
        if not config.get("authorized_github_usernames", []) and not config.get("editor_github_username", ""):
            log.warning("No authorized accounts in config.json - rejecting all logins")
            return web.Response(status=403, text="Dashboard not yet configured with any authorized accounts")
        log.warning(f"Rejected login attempt from unauthorized GitHub account: {github_username}")
        return web.Response(status=403, text="This GitHub account is not authorized for this dashboard")
    if role == "editor" and not any(path_matches(next_path.split("?")[0], p) for p in EDITOR_PATHS):
        next_path = "/editor"

    session_token = create_session(github_username, role)
    log.info(f"Dashboard login successful: {github_username}")

    # Back to whatever they were trying to open, not the homepage. Sending
    # them to "/" meant the hidden gesture had to be performed a second
    # time before /admin would actually open.
    response = web.HTTPFound(next_path)
    response.set_cookie(
        SESSION_COOKIE_NAME, session_token, httponly=True, samesite="Lax",
        secure=True, max_age=SESSION_MAX_AGE_SECONDS,
    )
    raise response


def is_authenticated(request: web.Request) -> bool:
    return session_for(request) is not None


async def handle_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        db.ensure(_SESSIONS_DDL)
        db.get().execute("DELETE FROM sessions WHERE token = ?", (token,))
    response = web.HTTPFound("/")
    response.del_cookie(SESSION_COOKIE_NAME)
    return response


def role_for_username(username: str) -> str | None:
    name = username.lower()
    if name in [u.lower() for u in config.get("authorized_github_usernames", [])]:
        return "admin"
    editor = config.get("editor_github_username", "")
    if editor and name == editor.lower():
        return "editor"
    return None


def path_matches(path: str, prefix: str) -> bool:
    """Whole-segment prefix: /clips matches /clips/a.mp4 but not /clipsadmin."""
    return path == prefix or path.startswith(prefix + "/")


OPEN_PATHS = (
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
    "/api/ocr/heartbeat",  # and again - the agent's liveness ping, same X-Agent-Secret check
    "/api/game/state",  # the Overwolf app on the gaming PC, same X-Agent-Secret
                        # check and the same reason: it has no GitHub session
                        # either, so without this line its own auth never runs.
    "/api/uploader",  # the gaming-PC VOD uploader, same X-Agent-Secret check, same reason
    "/",
)

EDITOR_PATHS = ("/editor", "/api/editor", "/auth/logout")


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
    # aiohttp does not collapse dot segments, so "/_next/../admin" would match
    # an open prefix while a proxy or the static handler resolves it elsewhere.
    if any(seg in (".", "..") for seg in request.path.split("/")):
        return web.Response(status=400, text="Bad path")

    if request.path == "/" or any(path_matches(request.path, p) for p in OPEN_PATHS if p != "/"):
        return await handler(request)

    session = session_for(request)
    if session is None:
        # Visiting /admin or /editor itself (not an API call) without a
        # session should smoothly redirect into the login flow rather than
        # show a bare 401 - someone who just did the hidden gesture is
        # expecting to land somewhere, not read an error.
        if path_matches(request.path, "/admin") or path_matches(request.path, "/editor"):
            raise web.HTTPFound(f"/auth/login?{urlencode({'next': request.path_qs})}")

        # An /api/ caller gets JSON, because it is code that will call
        # .json() on whatever comes back. This used to answer text/plain,
        # and every dashboard panel then reported the same useless
        # "JSON.parse: unexpected character at line 1 column 1" - which
        # says only "the body was not JSON" and is produced identically by
        # an expired session, a route this backend does not have, and a
        # Cloudflare error page while the backend is restarting. Three
        # completely different fixes behind one indistinguishable message.
        # Saying it in JSON costs nothing and puts the real reason on
        # screen.
        if request.path.startswith("/api/"):
            return web.json_response(
                {"error": "Not authenticated - open /auth/login in a browser tab, then retry."},
                status=401,
            )
        return web.Response(status=401, text="Not authenticated - visit /auth/login first")

    if session["role"] == "admin" or any(path_matches(request.path, p) for p in EDITOR_PATHS):
        return await handler(request)

    # An editor anywhere else: JSON for code, a bounce to their own pages
    # for a browser.
    if request.path.startswith("/api/"):
        return web.json_response({"error": "This account can only use the editor pages."}, status=403)
    raise web.HTTPFound("/editor")
