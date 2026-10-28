"""
The gaming-PC uploader's routes, under /api/uploader. The uploader has no GitHub session, so
auth.py's OPEN_PATHS lets the prefix through and every handler checks X-Agent-Secret itself -
the same ocr_agent_secret the OCR agent sends: one machine, one trust boundary, one secret.

Bytes never pass through here: `projects` hands back a Drive resumable-session URL and the PC
uploads straight to Google, then calls `complete`. Both are safe to repeat, because the PC
retries everything that did not get an answer (a power cut on either machine included).
"""
import asyncio
import functools
import hmac
import re
import time
from pathlib import Path

from aiohttp import web

import db
import deletions
import discord_live
import editor_service as svc
import editor_store as es
import gdrive
import twitch_client
from config import config
from logger import get_logger
from editor_api import _FILE_ID, _ID, _Fail, _body, _id, _int, _ok, _seconds, _text, map_errors

log = get_logger("UploaderAPI")

DELETED = "This VOD was deleted"
LIVE_CACHE_SECONDS = 60  # Twitch is asked at most this often, however often the PC polls
_live_cache: "tuple[float, bool] | None" = None
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Serialises project creation, so a retry that overlaps a slow first call (both awaiting Drive)
# cannot make two projects for one job_id.
_create_lock = asyncio.Lock()


def _secret_ok(request) -> bool:
    expected = config.get("ocr_agent_secret", "")
    provided = request.headers.get("X-Agent-Secret", "")
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


def _agent(handler):
    mapped = map_errors(handler)

    @functools.wraps(handler)
    async def wrapped(request):
        if not _secret_ok(request):
            return web.json_response({"error": "Invalid or missing agent secret"}, status=401)
        return await mapped(request)
    return wrapped


def _live_project(pid):
    """The project, or 409 once its VOD is being deleted or gone (the uploader drops the job)."""
    p = es.get_project(pid)
    if p is None:
        raise _Fail(404, "Not found")
    if p["deleted_at"] is not None or p["vod_status"] in ("deleting", "deleted"):
        raise _Fail(409, DELETED)
    return p


def _date(v):
    if not isinstance(v, str) or not _DATE.fullmatch(v):
        raise _Fail(400, "stream_date must be YYYY-MM-DD")
    try:
        time.strptime(v, "%Y-%m-%d")
    except ValueError:
        raise _Fail(400, "stream_date must be YYYY-MM-DD")
    return v


@_agent
async def create_project(request):
    body = await _body(request)
    job_id = body.get("job_id")
    if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
        raise _Fail(400, "job_id must be 32 lowercase hex characters")
    filename = Path(_text(body, "filename").replace("\\", "/")).name
    if not filename:
        raise _Fail(400, "filename must be a non-empty string")
    size = _int(body, "size")
    if size <= 0:
        raise _Fail(400, "size must be more than 0")
    duration_s = _seconds(body.get("duration_s"))
    stream_date = _date(body.get("stream_date"))
    mime = body.get("mime") or "video/mp4"
    if not isinstance(mime, str):
        raise _Fail(400, "mime must be a string")

    key = f"uploader_job_{job_id}"
    async with _create_lock:
        pid = db.meta_get(key)
        if pid is None:
            free = (await svc.free_space())["free"]
            if free is not None and size > free:
                raise svc.DriveFull(size, free)
            stem = Path(filename).stem
            p = es.create_project(stem, stream_date, discord_live.last_live_title(stream_date) or stem,
                                  filename, size, duration_s=duration_s)
            # create_project commits its own transaction; no await between it and this write.
            db.meta_set(key, str(p["id"]))
            pid = p["id"]
        p = _live_project(int(pid))
        if p["vod_status"] == "ready":
            return _ok({"project_id": p["id"], "kind": p["kind"], "status": "ready", "upload_url": ""})
        url = p["vod_upload_url"]
        if not url or body.get("new_session") is True:
            folder = await svc.ensure_project_folder(p["id"])
            deletions.ensure_project_not_deleting(p["id"])
            # The stored name and size, not the body's: vod_ready checks the file against vod_size.
            url = await gdrive.start_resumable_upload(p["vod_filename"], folder, p["vod_size"], mime,
                                                      {"project_id": str(p["id"])}, None)
            p = _live_project(p["id"])  # again after the awaits, right before the write
            deletions.ensure_project_not_deleting(p["id"])
            es.set_vod_upload(p["id"], url)
    return _ok({"project_id": p["id"], "kind": p["kind"], "status": p["vod_status"], "upload_url": url})


@_agent
async def complete(request):
    pid = _id(request)
    file_id = _text(await _body(request), "file_id")
    if not _FILE_ID.fullmatch(file_id):
        raise _Fail(400, "file_id is not a Drive file id")
    _live_project(pid)
    await svc.vod_ready(pid, file_id, "agent")
    return _ok({"status": "ready"})


@_agent
async def live(request):
    """Is the streamer live on Twitch right now? Asked directly (cached LIVE_CACHE_SECONDS), so it does
    not depend on go-live announcements being on; the announcer's last poll is the fallback."""
    global _live_cache
    channel = config.get("twitch_channel", "")
    now = time.time()
    if channel and (_live_cache is None or now - _live_cache[0] >= LIVE_CACHE_SECONDS):
        try:
            _live_cache = (now, await twitch_client.stream_info(channel) is not None)
        except Exception as e:
            log.warning("Twitch live check failed (%s); using the last poll", type(e).__name__)
            _live_cache = None
    return _ok({"live": _live_cache[1] if channel and _live_cache else discord_live.live_state()["live"]})


def register_routes(app: web.Application) -> None:
    r = app.router
    p = "/api/uploader"
    r.add_post(f"{p}/projects", create_project)
    r.add_post(f"{p}/projects/{{id:{_ID}}}/complete", complete)
    r.add_get(f"{p}/live", live)
