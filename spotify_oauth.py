"""
Spotify OAuth 2.0 - the one-time connection that produces a refresh token.

Same shape as streamlabs_oauth.py, and deliberately so: an admin-only
login redirect, a callback that exchanges the code immediately, and the
result written straight into config.json.

Two differences from the Streamlabs flow worth knowing about, because both
are things Spotify does and Streamlabs does not:

  - The token exchange authenticates with HTTP Basic (client id and secret
    base64'd into the Authorization header) and a form-encoded body, not a
    JSON body. Sending JSON here gets a 400 that says nothing useful.
  - The refresh token is the whole point. Spotify's access tokens last an
    hour, so unlike Streamlabs' non-expiring ones, the durable credential
    is the refresh token and it is what gets saved.

Gated behind the GitHub admin session like the Streamlabs flow - NOT in
auth.py's open_paths. Only the already-authenticated streamer should be
able to start a connection, and the callback is what turns a code into a
credential.

The redirect URI must match what is registered in the Spotify developer
dashboard EXACTLY, including scheme and trailing slash. Spotify is strict
about this in a way that produces an INVALID_CLIENT error naming nothing.
"""
import base64
import secrets

import aiohttp
from aiohttp import web

import spotify
from config import config
from logger import get_logger

log = get_logger("SpotifyAuth")

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"

_pending_states: set = set()


async def spotify_login(request: web.Request) -> web.Response:
    """Redirects the (already-authenticated admin) browser to Spotify's consent screen."""
    client_id = config.get("spotify_client_id", "")
    redirect_uri = config.get("spotify_redirect_uri", "")
    if not client_id or not redirect_uri:
        return web.json_response(
            {"error": "spotify_client_id and spotify_redirect_uri must be set in config.json first"},
            status=400,
        )

    state = secrets.token_urlsafe(24)
    _pending_states.add(state)

    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": spotify.SCOPES,
        "state": state,
        # Forces the consent screen even if this account has approved the
        # app before. Without it, re-running the flow to add a scope
        # silently returns a token with the OLD scopes, which then fails
        # at the first queue call with a 403 that looks like a Premium
        # problem.
        "show_dialog": "true",
    }
    raise web.HTTPFound(f"{AUTHORIZE_URL}?{urlencode(params)}")


async def spotify_callback(request: web.Request) -> web.Response:
    """Exchanges the authorization code for a refresh token, then stores it."""
    error = request.query.get("error")
    if error:
        log.error(f"Spotify authorization declined or failed: {error}")
        return web.json_response({"error": f"Spotify authorization failed: {error}"}, status=400)

    code = request.query.get("code")
    state = request.query.get("state")
    if not code:
        return web.json_response({"error": "Missing code parameter"}, status=400)
    if not state or state not in _pending_states:
        log.warning("Spotify callback with an unrecognized or reused state - possible CSRF, rejecting")
        return web.json_response({"error": "Invalid or expired state parameter"}, status=400)
    _pending_states.discard(state)

    client_id = config.get("spotify_client_id", "")
    client_secret = config.get("spotify_client_secret", "")
    redirect_uri = config.get("spotify_redirect_uri", "")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(spotify.TOKEN_URL, data=body, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200 or "refresh_token" not in data:
                log.error(f"Spotify token exchange failed: {resp.status} {data}")
                return web.json_response(
                    {"error": data.get("error_description") or data.get("error") or "Token exchange failed"},
                    status=400,
                )

    config.set("spotify_refresh_token", data["refresh_token"])
    config.save()
    # The in-memory access token belongs to whatever account was connected
    # before. Dropping it means the next request refreshes against the new
    # credential rather than using a stale one until it expires.
    spotify.forget_token()

    log.info("Spotify connected successfully - song requests are live")
    raise web.HTTPFound("/admin")
