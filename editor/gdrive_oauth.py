"""
Google Drive one-time connect, and the app's folder layout.

Same shape as spotify_oauth.py: an admin-only login redirect and a callback
that exchanges the code at once. Gated by the GitHub admin session - NOT in
auth.OPEN_PATHS and not in EDITOR_PATHS.
"""
import asyncio
import json
import secrets
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp import web

from core import db
from editor import gdrive
from core.config import config
from core.logger import get_logger

log = get_logger("GDriveAuth")

_MAX_PENDING_STATES = 32
# dict as an ordered set: the oldest state is dropped first when full.
_pending_states: dict = {}

# meta key -> (folder name, meta key of parent or None)
_LAYOUT = {
    "gdrive_root": ("DualBladeX Editor", None),
    "gdrive_projects": ("Projects", "gdrive_root"),
    "gdrive_brand_kit": ("Brand kit", "gdrive_root"),
    "gdrive_backups": ("Backups", "gdrive_root"),
}


def _remember_state(state: str) -> None:
    if len(_pending_states) >= _MAX_PENDING_STATES:
        _pending_states.pop(next(iter(_pending_states)))
    _pending_states[state] = None


# Every Drive folder creation (layout, project, tape) runs under this one lock,
# so two callers racing on a missing folder cannot create it twice.
_folder_lock = asyncio.Lock()


async def ensure_layout() -> "dict[str, str]":
    async with _folder_lock:
        return await ensure_layout_locked()


async def ensure_layout_locked() -> "dict[str, str]":
    """Creates whichever app folders are missing; the caller holds _folder_lock.
    Each id is saved as soon as it exists, so a failure partway leaves a retry
    only the rest to create."""
    ids: dict[str, str] = {}
    for key, (name, parent_key) in _LAYOUT.items():
        fid = db.meta_get(key)
        if not fid:
            fid = await gdrive.create_folder(name, ids[parent_key] if parent_key else None)
            db.meta_set(key, fid)
        ids[key] = fid
    email = config.get("editor_google_email", "")
    # Keyed by address, so changing editor_google_email shares with the new one.
    # ponytail: the previous address keeps its read access; revoke it in Drive by hand if that matters.
    if email and db.meta_get("gdrive_brand_kit_shared_with") != email:
        await gdrive.share_with(ids["gdrive_brand_kit"], email, "reader")
        db.meta_set("gdrive_brand_kit_shared_with", email)
    return ids


async def _note_account() -> None:
    """A reconnect to a different Google account cannot reach the old folders
    (drive.file only sees this app's files in *that* account), so forget them
    and let ensure_layout build the layout in the new one."""
    try:
        email = await gdrive.account_email()
    except gdrive.DriveError as e:
        log.warning(f"Could not read the connected Google account: {e}")
        return
    if not email:
        return
    previous = db.meta_get("gdrive_account")
    if previous and previous != email:
        log.warning("Drive reconnected to a different Google account - creating the folder layout there")
        with db.transaction():
            for key in (*_LAYOUT, "gdrive_brand_kit_shared_with"):
                db.meta_delete(key)
    db.meta_set("gdrive_account", email)


async def gdrive_login(request: web.Request) -> web.Response:
    client_id = config.get("gdrive_client_id", "")
    redirect_uri = config.get("gdrive_redirect_uri", "")
    missing = [k for k, v in (("gdrive_client_id", client_id), ("gdrive_redirect_uri", redirect_uri)) if not v]
    if missing:
        return web.json_response({"error": f"{', '.join(missing)} must be set in config.json first"}, status=400)
    state = secrets.token_urlsafe(24)
    _remember_state(state)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": gdrive.SCOPE,
        "access_type": "offline",
        # Without a fresh consent Google omits the refresh token on a repeat connect.
        "prompt": "consent",
        "state": state,
    }
    raise web.HTTPFound(f"{gdrive.AUTH_URL}?{urlencode(params, quote_via=quote)}")


async def gdrive_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error:
        log.error(f"Google authorization declined or failed: {error}")
        return web.json_response({"error": f"Google authorization failed: {error}"}, status=400)
    code = request.query.get("code")
    state = request.query.get("state")
    if not code:
        return web.json_response({"error": "Missing code parameter"}, status=400)
    if not state or state not in _pending_states:
        log.warning("Drive callback with an unrecognized or reused state - rejecting")
        return web.json_response({"error": "Invalid or expired state parameter"}, status=400)
    del _pending_states[state]

    body = {
        "code": code,
        "client_id": config.get("gdrive_client_id", ""),
        "client_secret": config.get("gdrive_client_secret", ""),
        "redirect_uri": config.get("gdrive_redirect_uri", ""),
        "grant_type": "authorization_code",
    }
    try:
        async with aiohttp.ClientSession(timeout=gdrive._TIMEOUT) as session:
            async with session.post(gdrive.TOKEN_URL, data=body) as resp:
                raw, status = await resp.read(), resp.status
    except Exception as e:
        log.error(f"Drive token exchange unreachable: {type(e).__name__}")
        return web.json_response({"error": "Could not reach Google to finish connecting"}, status=502)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if status != 200 or not data.get("refresh_token"):
        log.error(f"Drive token exchange failed: {status} {data.get('error', '')}")
        msg = data.get("error_description") or data.get("error") or "Token exchange failed"
        if status == 200:
            msg = ("Google returned no refresh token. Remove the app's access in your Google account "
                   "and run the connect again, approving the consent screen.")
        return web.json_response({"error": msg}, status=400)

    config.set("gdrive_refresh_token", data["refresh_token"])
    config.save()
    gdrive.forget_token()

    try:
        async with _folder_lock:
            await _note_account()
            await ensure_layout_locked()
    except gdrive.DriveError as e:
        log.error(f"Drive connected but folder setup failed: {e}")
        return web.json_response(
            {"error": "Drive is connected, but the folders couldn't be created. Run the connect again to retry."},
            status=502,
        )
    log.info("Google Drive connected - folder layout ready")
    raise web.HTTPFound("/admin")
