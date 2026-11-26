"""
The gaming-PC transcriber's routes, under /api/transcriber. Like uploader_api: no GitHub session, so the
prefix is in OPEN_PATHS and every handler checks X-Agent-Secret (ocr_agent_secret) itself.

The PC decides when it is idle; the backend decides whether it may work (the switch, not live) and hands
out one job at a time with a claim token. The Drive access token travels only in this JSON reply.
"""
import json
import time

from aiohttp import web

from core import db
from core.logger import get_logger
from editor import editor_store as es
from editor import gdrive
from editor import transcripts as tr
from editor import uploader_api
from editor.editor_api import _ID, _Fail, _body, _id, _ok

log = get_logger("TranscriberAPI")

MAX_BODY_BYTES = 4 * 1024 * 1024  # complete's segments; the app's default limit is 1 MiB
TAKEN_BACK = "This job is no longer yours"


async def _state():
    return tr.settings()["enabled"], await uploader_api.live_now()


@uploader_api._agent
async def next_job(request):
    db.meta_set("transcriber_last_seen", str(time.time()))
    enabled, live = await _state()
    job = None
    if enabled and live is not True:
        try:
            token = await gdrive.access_token()  # before the claim: nothing awaits between claim and reply
        except gdrive.DriveNotConnected:
            token = None
        claimed = tr.claim() if token else None
        if claimed:
            it = es.get_iteration(claimed["iteration_id"])
            job = {**claimed, "file_id": it["drive_file_id"], "access_token": token, "name": it["filename"],
                   "size": it["size"]}
            log.info("Transcript job claimed for version %s", it["id"])
    return _ok({"enabled": enabled, "live": live, "job": job})


@uploader_api._agent
async def status(request):
    enabled, live = await _state()
    return _ok({"enabled": enabled, "live": live})


async def _big_body(request) -> dict:
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data += chunk
        if len(data) > MAX_BODY_BYTES:
            raise _Fail(400, "The request body is too large")
    try:
        body = json.loads(data)
    except ValueError:
        raise _Fail(400, "The request body must be JSON")
    if not isinstance(body, dict):
        raise _Fail(400, "The request body must be a JSON object")
    return body


@uploader_api._agent
async def complete(request):
    iid = _id(request)
    body = await _big_body(request)
    if not isinstance(body.get("model"), str):
        raise _Fail(400, "model must be a string")
    if not tr.complete(iid, body.get("token"), body.get("segments"), body["model"]):  # ValueError -> 400
        log.info("Stale transcript for version %s refused", iid)
        raise _Fail(409, TAKEN_BACK)
    log.info("Transcript for version %s saved", iid)
    return _ok({"ok": True})


@uploader_api._agent
async def release(request):
    iid = _id(request)
    body = await _body(request)
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        raise _Fail(400, "error must be a string")
    if not tr.release(iid, body.get("token"), error):
        raise _Fail(409, TAKEN_BACK)
    log.info("Transcript job for version %s released%s", iid, " with an error" if error else "")
    return _ok({"ok": True})


def register_routes(app: web.Application) -> None:
    r = app.router
    p = "/api/transcriber"
    r.add_get(f"{p}/next", next_job)
    r.add_get(f"{p}/status", status)
    r.add_post(f"{p}/jobs/{{id:{_ID}}}/complete", complete)
    r.add_post(f"{p}/jobs/{{id:{_ID}}}/release", release)
