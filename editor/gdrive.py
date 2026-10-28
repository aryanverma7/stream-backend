"""
Google Drive client (drive.file scope). Metadata calls and upload sessions
only - video bytes go from the editor's browser straight to Google; the one
exception is upload_small_file, for backups of a few MB.

Never logs a token or a secret: errors carry the status and Google's
`error` field only.
"""
import asyncio
import json
import time
from pathlib import Path

import aiohttp

from core.config import config
from core.logger import get_logger

log = get_logger("GDrive")

SCOPE = "https://www.googleapis.com/auth/drive.file"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"

# Media links hand this token to the browser; leave it ten minutes of life when we do.
_TOKEN_EXPIRY_MARGIN_SECONDS = 600
_TIMEOUT = aiohttp.ClientTimeout(total=60)
# A backup PUT on a slow home uplink can legitimately take minutes: bound the
# stalls, not the total.
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
_FILE_FIELDS = "id,name,size,parents,appProperties,webViewLink"

_token: "str | None" = None
_token_expires_at: float = 0.0
_token_lock = asyncio.Lock()


class DriveNotConnected(Exception):
    """No refresh token, or Google refused it."""


class DriveError(Exception):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def forget_token() -> None:
    global _token, _token_expires_at
    _token = None
    _token_expires_at = 0.0


def token_expires_at() -> float:
    return _token_expires_at


async def access_token(now: "float | None" = None) -> str:
    global _token, _token_expires_at
    now = time.time() if now is None else now
    async with _token_lock:
        if _token and now < _token_expires_at:
            return _token
        refresh_token = config.get("gdrive_refresh_token", "")
        client_id = config.get("gdrive_client_id", "")
        client_secret = config.get("gdrive_client_secret", "")
        if not refresh_token or not client_id or not client_secret:
            raise DriveNotConnected("Google Drive isn't connected yet.")
        body = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(TOKEN_URL, data=body) as resp:
                    raw = await resp.read()
                    status = resp.status
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise DriveError(f"Could not reach Google: {type(e).__name__}", 0) from e
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        if status in (400, 401):
            log.error(f"Drive token refresh refused: {status} {data.get('error', '')}")
            raise DriveNotConnected("Google refused the saved login - reconnect Drive.")
        if status != 200 or "access_token" not in data:
            raise DriveError(f"Token refresh failed ({status})", status)
        _token = data["access_token"]
        _token_expires_at = now + int(data.get("expires_in", 3600)) - _TOKEN_EXPIRY_MARGIN_SECONDS
        # Google may rotate the refresh token; the old one then stops working.
        if data.get("refresh_token"):
            config.set("gdrive_refresh_token", data["refresh_token"])
            config.save()
            log.info("Google issued a new Drive refresh token - saved")
        return _token


async def _request(method: str, url: str, *, ok_404: bool = False, headers: "dict | None" = None,
                   timeout: "aiohttp.ClientTimeout" = _TIMEOUT, **kw):
    """One authenticated call. Returns (response headers, parsed JSON or None)."""
    for attempt in (0, 1):
        h = {"Authorization": f"Bearer {await access_token()}", **(headers or {})}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(method, url, headers=h, timeout=timeout, **kw) as resp:
                    raw = await resp.read()
                    status, rh = resp.status, resp.headers
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            raise DriveError(f"Could not reach Google Drive: {type(e).__name__}", 0) from e
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            data = None
        if status == 401 and attempt == 0:
            forget_token()
            continue
        if status == 404 and ok_404:
            return rh, None
        if status >= 300:
            err = data.get("error") if isinstance(data, dict) else None
            reason = err.get("message") if isinstance(err, dict) else err
            log.error(f"Drive {method} failed: {status} {reason}")
            raise DriveError(f"Drive request failed ({status})", status)
        return rh, data


def _file(f: dict) -> dict:
    if "size" in f:
        f["size"] = int(f["size"])
    return f


async def about() -> dict:
    _, d = await _request("GET", f"{API}/about", params={"fields": "storageQuota"})
    q = (d or {}).get("storageQuota", {})
    limit = q.get("limit")
    return {"limit": int(limit) if limit is not None else None, "usage": int(q.get("usage", 0))}


async def create_folder(name: str, parent_id: "str | None") -> str:
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    _, d = await _request("POST", f"{API}/files", params={"fields": "id"}, json=meta)
    return d["id"]


async def start_resumable_upload(name: str, parent_id: str, size: int, mime: str,
                                 app_properties: "dict[str, str]", origin: "str | None") -> str:
    headers = {"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime}
    if origin:
        headers["Origin"] = origin
    rh, _ = await _request(
        "POST", f"{UPLOAD_API}?uploadType=resumable", headers=headers,
        json={"name": name, "parents": [parent_id], "appProperties": app_properties},
    )
    loc = rh.get("Location")
    if not loc:
        raise DriveError("Drive gave no upload session URL", 0)
    return loc


async def get_file(file_id: str) -> dict:
    _, d = await _request("GET", f"{API}/files/{file_id}", params={"fields": _FILE_FIELDS})
    return _file(d)


async def list_folder(folder_id: str) -> "list[dict]":
    out, token = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": f"nextPageToken,files({_FILE_FIELDS})",
            "pageSize": "1000",
        }
        if token:
            params["pageToken"] = token
        _, d = await _request("GET", f"{API}/files", params=params)
        out += [_file(f) for f in (d or {}).get("files", [])]
        token = (d or {}).get("nextPageToken")
        if not token:
            return out


async def share_with(file_id: str, email: str, role: str = "reader") -> None:
    await _request("POST", f"{API}/files/{file_id}/permissions",
                   params={"sendNotificationEmail": "false"},
                   json={"type": "user", "role": role, "emailAddress": email})


async def delete_file(file_id: str) -> None:
    await _request("DELETE", f"{API}/files/{file_id}", ok_404=True)


async def upload_small_file(path: Path, name: str, parent_id: str,
                            mime: str = "application/octet-stream") -> str:
    data = Path(path).read_bytes()
    url = await start_resumable_upload(name, parent_id, len(data), mime, {}, None)
    # The session URL carries its own authorisation; a bearer header is harmless.
    _, d = await _request("PUT", url, data=data,
                          headers={"Content-Length": str(len(data))}, timeout=_UPLOAD_TIMEOUT)
    return d["id"]
