"""
Streamlabs OAuth 2.0 flow (Task #5) - login redirect + token exchange.

Every endpoint and requirement below was verified directly against the
CURRENT (v2.0) official docs at dev.streamlabs.com on 2026-08-24, not
assumed from training data or older cached docs - the API has both a v1.0
and v2.0 doc set still floating around online with different endpoint
paths, and mixing them would mean this silently doesn't work.

Confirmed facts driving the implementation below:
  - Authorize: GET https://streamlabs.com/api/v2.0/authorize
    (response_type, client_id, redirect_uri, scope, state)
  - Token exchange: POST https://streamlabs.com/api/v2.0/token, JSON body,
    REQUIRES both Content-Type: application/json AND
    X-Requested-With: XMLHttpRequest headers - omitting either causes the
    request to fail.
  - Access tokens do NOT expire ("Do Not Refresh Tokens" - Streamlabs'
    own docs) - no refresh-token background job is needed. The
    refresh_token is still stored, in case that policy ever changes.
  - Authorization codes expire after 5 minutes and are single-use - the
    callback below exchanges the code immediately, not deferred.
  - Scopes requested: socket.token (Task #6's real-time tips listener),
    points.read + points.write (already used by the existing, tested
    points.py grant/balance functions) - NOT donations.read/create, since
    we listen for donations via the Socket API, not by polling REST.

This entire flow is gated behind the existing GitHub OAuth admin session
(NOT added to auth.py's open_paths) - only the already-authenticated
streamer should ever be able to trigger a connection attempt, not any
random visitor to the site.
"""
import secrets

import aiohttp
from aiohttp import web

from config import config
from logger import get_logger
from streamlabs_socket import start_tips_listener, stop_tips_listener

log = get_logger("StreamlabsAuth")

AUTHORIZE_URL = "https://streamlabs.com/api/v2.0/authorize"
TOKEN_URL = "https://streamlabs.com/api/v2.0/token"
SCOPES = "socket.token points.read points.write"

_pending_states: set[str] = set()


async def streamlabs_login(request: web.Request) -> web.Response:
    """Redirects the (already-authenticated admin) browser to Streamlabs' consent screen."""
    client_id = config.get("streamlabs_client_id", "")
    redirect_uri = config.get("streamlabs_redirect_uri", "")
    if not client_id or not redirect_uri:
        return web.json_response(
            {"error": "streamlabs_client_id and streamlabs_redirect_uri must be set in config.json first"},
            status=400,
        )

    state = secrets.token_urlsafe(24)
    _pending_states.add(state)

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    raise web.HTTPFound(f"{AUTHORIZE_URL}?{query}")


async def streamlabs_callback(request: web.Request) -> web.Response:
    """Exchanges the authorization code for an access token, then stores it in config.json."""
    error = request.query.get("error")
    if error:
        log.error(f"Streamlabs authorization declined or failed: {error}")
        return web.json_response({"error": f"Streamlabs authorization failed: {error}"}, status=400)

    code = request.query.get("code")
    state = request.query.get("state")

    if not code:
        return web.json_response({"error": "Missing code parameter"}, status=400)

    if not state or state not in _pending_states:
        log.warning("Streamlabs callback received with an unrecognized or reused state - possible CSRF, rejecting")
        return web.json_response({"error": "Invalid or expired state parameter"}, status=400)
    _pending_states.discard(state)

    client_id = config.get("streamlabs_client_id", "")
    client_secret = config.get("streamlabs_client_secret", "")
    redirect_uri = config.get("streamlabs_redirect_uri", "")

    body = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    headers = {
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(TOKEN_URL, json=body, headers=headers) as resp:
            data = await resp.json()
            if resp.status != 200 or "access_token" not in data:
                log.error(f"Streamlabs token exchange failed: {resp.status} {data}")
                return web.json_response({"error": data.get("error", "Token exchange failed")}, status=502)

    config.set("streamlabs_access_token", data["access_token"])
    if "refresh_token" in data:
        config.set("streamlabs_refresh_token", data["refresh_token"])
    config.save()

    log.info("Streamlabs account connected successfully")

    # ADDED: without this, the running backend never learns a token just
    # became available - it only ever checked for one at startup, meaning
    # a fresh connection made mid-session would silently sit unused until
    # the next manual restart. stop_tips_listener() first (harmless no-op
    # if nothing was running) so reconnecting with a new token doesn't
    # leave two socket connections open at once.
    try:
        await stop_tips_listener()
        await start_tips_listener()
    except Exception:
        log.exception("Connected to Streamlabs, but could not start the tips listener - check the socket API connection")

    raise web.HTTPFound("/admin")
