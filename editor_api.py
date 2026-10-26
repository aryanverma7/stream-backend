"""
Every /api/editor/* route. auth_middleware already lets both roles in (EDITOR_PATHS);
anything only the streamer may do is checked HERE, never left to the UI.

Handlers stay thin: parse, call one module function, map its exceptions.
"""
import base64
import binascii
import functools
import json
import math
import re
import time

from aiohttp import web

import auth
import db
import deletions
import editor_bell
import editor_comments as ec
import editor_personal as ep
import editor_service as svc
import editor_store as es
import gdrive
import gdrive_oauth
import notify_outbox
import scheduler

ONLY_STREAMER = "Only the streamer can do this."
NOT_CONNECTED = "Google Drive isn't connected - connect it from the dashboard."
DRIVE_PROBLEM = "Google Drive had a problem - try again."


class _Fail(Exception):
    def __init__(self, status, error, **extra):
        super().__init__(error)
        self.status = status
        self.payload = {"error": error, **extra}


def _route(handler):
    """Runs a handler as (request, me) and turns every expected failure into a JSON error."""
    @functools.wraps(handler)
    async def wrapped(request):
        me = auth.session_for(request)
        if me is None:  # the middleware already refused these; belt and braces
            return web.json_response({"error": "Not authenticated"}, status=401)
        try:
            return await handler(request, me)
        except _Fail as e:
            return web.json_response(e.payload, status=e.status)
        except svc.DriveFull as e:
            return web.json_response({"error": "Drive is too full for this file", "needed": e.needed,
                                      "free": e.free}, status=507)
        except gdrive.DriveNotConnected:
            return web.json_response({"error": NOT_CONNECTED}, status=503)
        except gdrive.DriveError:
            return web.json_response({"error": DRIVE_PROBLEM}, status=502)
        except deletions.NotFound as e:
            return web.json_response({"error": str(e)}, status=404)
        except deletions.ConfirmMismatch:
            return web.json_response({"error": "Type the name exactly to confirm"}, status=400)
        except PermissionError as e:  # includes deletions.NotAllowed
            return web.json_response({"error": str(e) or ONLY_STREAMER}, status=403)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
    return wrapped


def _admin(me):
    if me["role"] != "admin":
        raise _Fail(403, ONLY_STREAMER)


_ID = r"\d{1,18}"  # fits SQLite's signed 64-bit INTEGER; longer is simply not found
_FILE_ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


def _id(request, key="id"):
    return int(request.match_info[key])  # routes only match _ID


def _found(row):
    if row is None:
        raise _Fail(404, "Not found")
    return row


async def _body(request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise _Fail(400, "The request body must be JSON")
    if not isinstance(body, dict):
        raise _Fail(400, "The request body must be a JSON object")
    return body


def _text(body, key, *, required=True):
    v = body.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise _Fail(400, f"{key} must be a non-empty string")
    return v.strip()


def _seconds(v):
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise _Fail(400, "Times must be numbers of seconds")
    return float(v)


def _int(body, key):
    v = body.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise _Fail(400, f"{key} must be a whole number")
    return v


def _ok(data, status=200):
    return web.json_response(data, status=status)


def _project(id):
    p = es.get_project(id)
    return _found(None if p is None or p["deleted_at"] is not None else p)


def _project_view(p):
    return {k: v for k, v in p.items() if k != "vod_upload_url"}


def _tape(id):
    """A tape, 404 if it or its project is gone."""
    t = _found(es.get_tape(id))
    _project(t["project_id"])
    return t


def _iteration(id):
    it = _found(es.get_iteration(id))
    _tape(it["tape_id"])
    return it


def _comment(id):
    c = _found(ec._comment(id))
    _iteration(c["iteration_id"])
    return c


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"  # carries a live access token
    return resp


def _latest_ready(tape_id):
    return next((i for i in reversed(es.list_iterations(tape_id)) if i["status"] == "ready"), None)


def _iteration_view(it, with_upload_url=False):
    """The upload URL is a bearer credential for the upload: only the single-iteration GET carries it."""
    view = {k: v for k, v in it.items() if k != "thumbnail" and (with_upload_url or k != "upload_url")}
    view["has_thumbnail"] = it.get("thumbnail") is not None
    view["checks"] = json.loads(it["checks_json"]) if it.get("checks_json") else None
    return view


# -- who am I --

@_route
async def me(request, me):
    return _ok({"username": me["username"], "role": me["role"]})


# -- projects --

@_route
async def list_projects(request, me):
    projects = [{**_project_view(p), "has_new": any(ep.tape_has_new(me["username"], t["id"]) for t in es.list_tapes(p["id"]))}
                for p in es.list_projects()]
    return _ok({"projects": projects})


@_route
async def get_project(request, me):
    p = _project(_id(request))
    tapes = []
    for t in es.list_tapes(p["id"]):
        latest = _latest_ready(t["id"])
        open_comments = 0 if latest is None else sum(
            not c["resolved"] for c in ec.comments_for_iteration(latest["id"]))
        tapes.append({**t, "has_new": ep.tape_has_new(me["username"], t["id"]),
                      "latest_version": latest and latest["version"],
                      "latest_iteration_id": latest and latest["id"], "open_comments": open_comments})
    return _ok({"project": _project_view(p), "tapes": tapes})


@_route
async def rename_project(request, me):
    _admin(me)
    p = _project(_id(request))
    return _ok(_project_view(es.rename_project(p["id"], _text(await _body(request), "name"))))


@_route
async def download_project(request, me):
    p = _project(_id(request))
    link = await svc.media_link(project_id=p["id"])
    # Only the editor's click counts: it is what lets the editor delete the VOD afterwards.
    if me["role"] == "editor":
        es.record_download_click(p["id"])
    return _no_store(_ok(link))


@_route
async def create_tape(request, me):
    p = _project(_id(request))
    return _ok(svc.create_tape(p["id"], _text(await _body(request), "name"), me["username"]), 201)


# -- clips and reels --

@_route
async def combine_clips(request, me):
    body = await _body(request)
    ids = body.get("project_ids")
    if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise _Fail(400, "project_ids must be a list of whole numbers")
    return _ok(svc.combine_clips(ids, _text(body, "name"), me["username"]))


@_route
async def uncombine(request, me):
    t = _tape(_id(request))
    return _ok({"reels": svc.uncombine(t["id"], me["username"])})


# -- tapes --

@_route
async def get_tape(request, me):
    t = _tape(_id(request))
    ep.mark_tape_seen(me["username"], t["id"])
    return _ok({"tape": t, "iterations": [_iteration_view(i) for i in es.list_iterations(t["id"])]})


@_route
async def update_tape(request, me):
    t = _tape(_id(request))
    body = await _body(request)
    status = body.get("status")
    # Finalising, or undoing a final, is the streamer's call.
    if status is not None and me["role"] != "admin" and (status == "final" or t["status"] == "final"):
        raise _Fail(403, ONLY_STREAMER)
    notes = body.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise _Fail(400, "notes must be a string")
    return _ok(es.update_tape(t["id"], name=_text(body, "name", required=False), notes=notes, status=status))


@_route
async def start_iteration(request, me):
    t = _tape(_id(request))
    body = await _body(request)
    size = _int(body, "size")
    if size <= 0:
        raise _Fail(400, "size must be positive")
    return _ok(await svc.start_iteration_upload(t["id"], _text(body, "filename"), size, _text(body, "mime")))


# -- iterations --

@_route
async def complete_iteration(request, me):
    it = _iteration(_id(request))
    file_id = _text(await _body(request), "file_id")
    if not _FILE_ID.fullmatch(file_id):
        raise _Fail(400, "file_id is not a Drive file id")
    return _ok(_iteration_view(await svc.complete_iteration_upload(it["id"], file_id, me["username"])))


@_route
async def start_over(request, me):
    _admin(me)
    it = _iteration(_id(request))
    return _ok({"resolved": ec.start_over(it["id"], me["username"])})


@_route
async def get_iteration(request, me):
    it = _iteration(_id(request))
    return _ok({"iteration": _iteration_view(it, with_upload_url=True), "comments": ec.comments_for_iteration(it["id"]),
                "position_s": ep.get_position(me["username"], it["id"])})


@_route
async def get_thumbnail(request, me):
    it = _iteration(_id(request))
    return web.Response(body=_found(it["thumbnail"]), content_type="image/jpeg",
                        headers={"X-Content-Type-Options": "nosniff"})


@_route
async def put_media(request, me):
    it = _iteration(_id(request))
    body = await _body(request)
    checks_json = thumbnail = None
    if body.get("checks") is not None:
        checks_json = json.dumps(body["checks"])
        if len(checks_json.encode()) > es.CHECKS_MAX_BYTES:
            raise _Fail(413, "The checks are too large")
    if body.get("thumbnail_b64") is not None:
        try:
            thumbnail = base64.b64decode(body["thumbnail_b64"], validate=True)
        except (binascii.Error, TypeError, ValueError):
            raise _Fail(400, "The thumbnail is not valid base64")
        if len(thumbnail) > es.THUMBNAIL_MAX_BYTES:
            raise _Fail(413, "The thumbnail is too large")
    return _ok(_iteration_view(es.set_iteration_media(it["id"], checks_json=checks_json, thumbnail=thumbnail)))


@_route
async def media_link(request, me):
    it = _iteration(_id(request))
    return _no_store(_ok(await svc.media_link(iteration_id=it["id"])))


@_route
async def put_position(request, me):
    it = _iteration(_id(request))
    pos = _seconds((await _body(request)).get("position_s"))
    if pos is None or pos < 0:
        raise _Fail(400, "position_s must be a number of seconds")
    ep.set_position(me["username"], it["id"], pos)
    return _ok({"position_s": pos})


# -- comments --

@_route
async def add_comment(request, me):
    it = _iteration(_id(request))
    body = await _body(request)
    if body.get("kind") not in ec.KINDS:
        raise _Fail(400, "kind must be one of fix, keep, question")
    text = body.get("body")
    if not isinstance(text, str):
        raise _Fail(400, "body must be a string")
    return _ok(ec.add_comment(it["id"], me["username"], body["kind"], text,
                              _seconds(body.get("start_s")), _seconds(body.get("end_s"))), 201)


@_route
async def edit_comment(request, me):
    c = _comment(_id(request))
    body = await _body(request)
    for key in ("body", "kind"):
        if body.get(key) is not None and not isinstance(body[key], str):
            raise _Fail(400, f"{key} must be a string")
    return _ok(ec.edit_comment(c["id"], me["username"], body=body.get("body"), kind=body.get("kind")))


@_route
async def add_reply(request, me):
    c = _comment(_id(request))
    text = (await _body(request)).get("body")
    if not isinstance(text, str):
        raise _Fail(400, "body must be a string")
    return _ok(ec.add_reply(c["id"], me["username"], text), 201)


@_route
async def set_resolved(request, me):
    _admin(me)
    it = _iteration(_id(request, "iid"))
    resolved = (await _body(request)).get("resolved")
    if not isinstance(resolved, bool):
        raise _Fail(400, "resolved must be true or false")
    state = ec.set_resolved(_id(request, "cid"), it["id"], resolved, me["username"])
    return _ok({**state, "resolved": bool(state["resolved"])})


@_route
async def search(request, me):
    return _ok({"hits": ec.search(request.query.get("q", ""))})


# -- notification bell (both roles; whose bell is always the session's, never a parameter) --

def _non_negative_int(v):
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 10**18:  # 18 digits, as _ID
        raise _Fail(400, "Expected a non-negative integer id")
    return v


@_route
async def notifications(request, me):
    after = request.query.get("after")
    if after is not None:
        if not after.isdigit() or len(after) > 18:
            raise _Fail(400, "after must be an event id")
        after = int(after)
    return _ok(editor_bell.feed(me["username"], after_id=after))


@_route
async def notifications_seen(request, me):
    editor_bell.mark_seen(me["username"], _non_negative_int((await _body(request)).get("up_to_id")))
    return _ok({"ok": True})


# -- to-dos (owner only: another user's id is simply not found) --

@_route
async def list_todos(request, me):
    return _ok({"todos": ep.list_todos(me["username"])})


@_route
async def add_todo(request, me):
    return _ok(ep.add_todo(me["username"], (await _body(request)).get("text")), 201)


@_route
async def update_todo(request, me):
    body = await _body(request)
    done = body.get("done")
    if done is not None and not isinstance(done, bool):
        raise _Fail(400, "done must be true or false")
    return _ok(_found(ep.update_todo(me["username"], _id(request), text=body.get("text"), done=done)))


@_route
async def delete_todo(request, me):
    if not ep.delete_todo(me["username"], _id(request)):
        raise _Fail(404, "Not found")
    return _ok({"deleted": True})


# -- deletions (deletions.py enforces who may delete and undo what) --

@_route
async def request_delete(request, me):
    body = await _body(request)
    confirm = body.get("confirm")
    if not isinstance(confirm, str):
        raise _Fail(400, "confirm must be a string")
    return _ok(deletions.request_delete(body.get("kind"), _int(body, "target_id"), confirm,
                                        username=me["username"], role=me["role"]), 202)


@_route
async def undo_delete(request, me):
    return _ok(deletions.undo(_id(request), username=me["username"], role=me["role"]))


@_route
async def pending_deletes(request, me):
    return _ok({"pending": deletions.pending()})


# -- admin: storage, scheduler, Drive --

@_route
async def storage(request, me):
    _admin(me)
    return _ok({**await svc.storage_view(), "scheduler": scheduler.status(),
                "discord": notify_outbox.status()})


@_route
async def reset_clock(request, me):
    _admin(me)
    now = time.time()
    scheduler.reset_clock(now)
    deletions.clamp_after_clock_reset(now)  # here, not in scheduler: it must not import deletions
    return _ok(scheduler.status())


@_route
async def drive_status(request, me):
    _admin(me)
    out = {"connected": True, "layout": all(db.meta_get(k) for k in gdrive_oauth._LAYOUT)}
    try:
        out["quota"] = await svc.free_space()
    except gdrive.DriveNotConnected:
        out["connected"] = False
    except gdrive.DriveError:
        out["error"] = DRIVE_PROBLEM
    return _ok(out)


def register_routes(app: web.Application) -> None:
    r = app.router
    p = "/api/editor"
    r.add_get(f"{p}/me", me)
    r.add_get(f"{p}/projects", list_projects)
    r.add_get(f"{p}/projects/{{id:{_ID}}}", get_project)
    r.add_patch(f"{p}/projects/{{id:{_ID}}}", rename_project)
    r.add_post(f"{p}/projects/{{id:{_ID}}}/download", download_project)
    r.add_post(f"{p}/projects/{{id:{_ID}}}/tapes", create_tape)
    r.add_post(f"{p}/clips/combine", combine_clips)
    r.add_post(f"{p}/tapes/{{id:{_ID}}}/uncombine", uncombine)
    r.add_get(f"{p}/tapes/{{id:{_ID}}}", get_tape)
    r.add_patch(f"{p}/tapes/{{id:{_ID}}}", update_tape)
    r.add_post(f"{p}/tapes/{{id:{_ID}}}/iterations", start_iteration)
    r.add_post(f"{p}/iterations/{{id:{_ID}}}/complete", complete_iteration)
    r.add_post(f"{p}/iterations/{{id:{_ID}}}/start-over", start_over)
    r.add_get(f"{p}/iterations/{{id:{_ID}}}", get_iteration)
    r.add_get(f"{p}/iterations/{{id:{_ID}}}/thumbnail", get_thumbnail)
    r.add_put(f"{p}/iterations/{{id:{_ID}}}/media", put_media)
    r.add_get(f"{p}/iterations/{{id:{_ID}}}/media-link", media_link)
    r.add_put(f"{p}/iterations/{{id:{_ID}}}/position", put_position)
    r.add_post(f"{p}/iterations/{{id:{_ID}}}/comments", add_comment)
    r.add_patch(f"{p}/comments/{{id:{_ID}}}", edit_comment)
    r.add_post(f"{p}/comments/{{id:{_ID}}}/replies", add_reply)
    r.add_put(f"{p}/iterations/{{iid:{_ID}}}/comments/{{cid:{_ID}}}/resolved", set_resolved)
    r.add_get(f"{p}/search", search)
    r.add_get(f"{p}/notifications", notifications)
    r.add_post(f"{p}/notifications/seen", notifications_seen)
    r.add_get(f"{p}/todos", list_todos)
    r.add_post(f"{p}/todos", add_todo)
    r.add_patch(f"{p}/todos/{{id:{_ID}}}", update_todo)
    r.add_delete(f"{p}/todos/{{id:{_ID}}}", delete_todo)
    r.add_get(f"{p}/deletions", pending_deletes)
    r.add_post(f"{p}/deletions", request_delete)
    r.add_post(f"{p}/deletions/{{id:{_ID}}}/undo", undo_delete)
    r.add_get(f"{p}/storage", storage)
    r.add_post(f"{p}/scheduler/reset-clock", reset_clock)
    r.add_get(f"{p}/drive/status", drive_status)
