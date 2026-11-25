"""Drive-backed editor flows. Video bytes never touch this machine: we open a
resumable-upload session and hand the browser its URL."""
import asyncio
import time

from core.config import config
from core import db
from editor import deletions
from editor import editor_attachments as ea
from editor import editor_comments
from editor import editor_store as es
from editor import gdrive
from editor import gdrive_oauth
from core.logger import get_logger

log = get_logger("EditorService")


class DriveFull(Exception):
    def __init__(self, needed: int, free: int):
        super().__init__(f"Drive has {free} bytes free but this file needs {needed}.")
        self.needed = needed
        self.free = free


async def free_space() -> dict:
    a = await gdrive.about()
    limit, usage = a["limit"], a["usage"]
    return {"limit": limit, "usage": usage, "free": None if limit is None else max(0, limit - usage)}


class Unavailable(Exception):
    """A file the route cannot hand out; editor_api answers it with `status` and this message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


_folder_lock = gdrive_oauth._folder_lock  # one lock for every folder creation


async def _project_folder(project_id) -> str:
    p = es.get_project(project_id)
    if p is None:
        raise ValueError("No such project")
    if p["drive_folder_id"]:
        return p["drive_folder_id"]
    parent = db.meta_get("gdrive_projects")
    if not parent:
        parent = (await gdrive_oauth.ensure_layout_locked())["gdrive_projects"]
    fid = await gdrive.create_folder(f"{p['stream_date']} {p['name']}", parent)
    es.set_project_folder(project_id, fid)
    return fid


async def ensure_project_folder(project_id) -> str:
    async with _folder_lock:
        return await _project_folder(project_id)


async def ensure_edit_folder(edit_id) -> str:
    async with _folder_lock:  # re-read inside the lock so a racing caller sees the saved id
        t = es.get_edit(edit_id)
        if t is None:
            raise ValueError("No such video, clip or reel")
        if t["drive_folder_id"]:
            return t["drive_folder_id"]
        fid = await gdrive.create_folder(t["name"], await _project_folder(t["project_id"]))
        es.set_edit_folder(edit_id, fid)
        return fid


def create_edit(project_id, name, actor) -> dict:
    deletions.ensure_project_not_deleting(project_id)
    with db.transaction():
        t = es.create_edit(project_id, name)
        es.record_event(actor, "edit_created", f"{actor} added {name}", project_id=project_id, edit_id=t["id"])
    return t


def _has_versions(edit_id) -> bool:
    return db.get().execute("SELECT 1 FROM iterations WHERE edit_id=? LIMIT 1", (edit_id,)).fetchone() is not None


def combine_tapes(project_ids, name, actor, now=None) -> dict:
    """
    Merges 2+ tapes into the first tape's reel, renamed to `name`; the other reels (empty) are deleted.
    Every tape already on an involved reel must be listed, so no tape ends up in two reels and no
    reel is left half-emptied. Refused once any involved reel has a version (even an unfinished upload).
    """
    now = time.time() if now is None else now
    ids = list(dict.fromkeys(project_ids))
    if len(ids) < 2:
        raise ValueError("Pick at least two tapes to combine")
    deletions._c()  # tables before the transaction: ensure() COMMITs
    with db.transaction() as conn:
        reels = []
        for pid in ids:
            p = es.get_project(pid)
            if p is None or p["deleted_at"] is not None or p["kind"] != "tape":
                raise ValueError("Only tapes can be combined")
            deletions.ensure_project_not_deleting(pid)
            if deletions.has_live_request("vod", pid):  # its file is going: nothing left to cut a reel from
                raise ValueError(f"{p['name']} is being deleted - it cannot be combined")
            reel = es.reel_for_tape(pid)
            if reel["id"] not in [r["id"] for r in reels]:
                reels.append(reel)
        for r in reels:
            if _has_versions(r["id"]):
                raise ValueError(f"{r['name']} already has a version - tapes can only be combined before that")
            if not set(r["source_project_ids"]) <= set(ids):
                raise ValueError(f"{r['name']} holds other tapes too - include them all, or uncombine it first")
        keep = reels[0]["id"]
        conn.execute("UPDATE reel_sources SET edit_id=? WHERE edit_id IN (%s)" % ",".join("?" * len(reels)),
                     (keep, *(r["id"] for r in reels)))
        for r in reels[1:]:
            conn.execute("DELETE FROM edits WHERE id=?", (r["id"],))
        conn.execute("UPDATE edits SET name=?, updated_at=? WHERE id=?", (name, now, keep))
        es.record_event(actor, "reels_combined", f"{actor} combined {len(ids)} tapes into {name}",
                        project_id=reels[0]["project_id"], edit_id=keep, now=now)
    return es.get_edit(keep)


def uncombine(edit_id, actor, now=None) -> list[dict]:
    """Gives every tape on a combined reel its own reel back; the hosting tape keeps this one."""
    now = time.time() if now is None else now
    deletions._c()  # tables before the transaction: ensure() COMMITs
    with db.transaction() as conn:
        t = es.get_edit(edit_id)
        if t is None or t["kind"] != "reel" or len(t["source_project_ids"]) < 2:
            raise ValueError("That is not a combined reel")
        if _has_versions(edit_id):
            raise ValueError(f"{t['name']} already has a version - it can only be uncombined before that")
        host = es.get_project(t["project_id"])
        conn.execute("UPDATE edits SET name=?, updated_at=? WHERE id=?", (host["name"], now, edit_id))
        out = [edit_id]
        for pid in t["source_project_ids"]:
            if pid == host["id"]:
                continue
            p = es.get_project(pid)
            conn.execute("DELETE FROM reel_sources WHERE project_id=?", (pid,))
            if p["deleted_at"] is None:  # a deleted tape just leaves the reel
                deletions.ensure_project_not_deleting(pid)
                out.append(es.insert_reel(conn, pid, p["name"], now))
        es.record_event(actor, "reels_uncombined", f"{actor} uncombined {t['name']}",
                        project_id=host["id"], edit_id=edit_id, now=now)
    return [es.get_edit(i) for i in out]


def _project_of(edit_id) -> int:
    t = es.get_edit(edit_id)
    if t is None:
        raise ValueError("No such video, clip or reel")
    return t["project_id"]


async def _upload_session(it, folder, mime) -> str:
    return await gdrive.start_resumable_upload(
        f"v{it['version']} - {it['filename']}", folder, it["size"], mime,
        {"iteration_id": str(it["id"])}, config.get("public_base_url") or None)


def _ensure_edit_open(edit_id) -> None:
    """No new version on an edit whose files are being, or have been, deleted."""
    deletions.ensure_project_not_deleting(_project_of(edit_id))
    deletions.ensure_edit_not_deleting(edit_id)
    if es.get_edit(edit_id)["files_deleted_at"] is not None:
        raise ValueError("These files were deleted")


async def start_iteration_upload(edit_id, filename, size, mime, shape=None) -> dict:
    """`shape` (width, height, duration_s) decides or must keep the edit's kind (es.detect_kind)."""
    _ensure_edit_open(edit_id)
    free = (await free_space())["free"]
    if free is not None and size > free:
        raise DriveFull(size, free)
    folder = await ensure_edit_folder(edit_id)
    # Again after the awaits, with none before the insert: once the row exists, a project delete refuses.
    _ensure_edit_open(edit_id)
    es._c()
    # The kind is checked and saved with the version row: two starts racing on a new output cannot both win.
    with db.transaction():
        was = es.get_edit(edit_id)["kind"]
        kind = es.detect_kind(es.get_edit(edit_id), *shape) if shape else None
        it = es.create_iteration(edit_id, filename, size, "")
    try:
        url = await _upload_session(it, folder, mime)
    except BaseException:
        with db.transaction() as conn:
            conn.execute("DELETE FROM iterations WHERE id=?", (it["id"],))
            if kind is not None and kind != was:  # no version came of it: the kind it set goes too
                conn.execute("UPDATE edits SET kind=? WHERE id=? AND kind=?", (was, edit_id, kind))
        raise
    with db.transaction() as conn:
        conn.execute("UPDATE iterations SET upload_url=? WHERE id=?", (url, it["id"]))
    return {"iteration_id": it["id"], "version": it["version"], "upload_url": url, "kind": kind}


async def renew_iteration_upload(iteration_id, mime) -> "str | None":
    """A fresh session for a version still uploading or abandoned (Google expires one after ~a week); None otherwise."""
    it = es.get_iteration(iteration_id)
    if it is None or it["status"] not in ("uploading", "abandoned"):
        return None
    project_id = _project_of(it["edit_id"])
    deletions.ensure_project_not_deleting(project_id)
    deletions.ensure_edit_not_deleting(it["edit_id"])
    es.ensure_kind_kept(it)
    url = await _upload_session(it, await ensure_edit_folder(it["edit_id"]), mime)
    deletions.ensure_project_not_deleting(project_id)  # after the awaits, right before the write
    deletions.ensure_edit_not_deleting(it["edit_id"])
    with db.transaction() as conn:
        es.ensure_kind_kept(it)
        # updated_at restarts reconcile's 8-day abandon clock: a renewal is someone still uploading.
        if not conn.execute("UPDATE iterations SET upload_url=?, status='uploading', updated_at=? WHERE id=?"
                            " AND status IN ('uploading','abandoned')",
                            (url, time.time(), iteration_id)).rowcount:
            return None
    return url


async def complete_iteration_upload(iteration_id, file_id, actor) -> dict:
    it = es.get_iteration(iteration_id)
    if it is None:
        raise ValueError("No such iteration")
    edit = es.get_edit(it["edit_id"])
    if edit is None:
        raise ValueError("No such video, clip or reel")
    deletions.ensure_edit_not_deleting(it["edit_id"])
    f = await gdrive.get_file(file_id)
    if (not edit["drive_folder_id"] or edit["drive_folder_id"] not in f.get("parents", [])
            or f.get("appProperties", {}).get("iteration_id") != str(iteration_id)
            or f.get("size") != it["size"]):
        raise ValueError("That file is not this iteration's upload")
    deletions.ensure_project_not_deleting(edit["project_id"])  # after the await, right before the write
    deletions.ensure_edit_not_deleting(it["edit_id"])
    row, _ = es.complete_iteration(iteration_id, file_id, actor=actor)  # records iteration_ready atomically
    # Every time, not only on the transition: idempotent, so a retry after a crash here still carries over.
    editor_comments.carry_over(iteration_id)
    # The editor's own Google account opens per-version links; without this Drive asks him to request access.
    # Every completion (re-sharing is harmless), and never a reason to fail one.
    email = config.get("editor_google_email", "")
    if email:
        try:
            await gdrive.share_with(file_id, email, "reader")
        except Exception as e:
            log.warning(f"could not share iteration {iteration_id} with the editor: {e}")
    return row


async def start_attachment_upload(iteration_id, author, name, size, mime) -> dict:
    """A resumable Drive session for one image on this iteration's edit: {id, upload_url}."""
    name = ea.validate(name, size, mime)
    it = es.get_iteration(iteration_id)
    if it is None:
        raise ValueError("No such iteration")
    edit_id = it["edit_id"]
    _ensure_edit_open(edit_id)
    parent = await ensure_edit_folder(edit_id)
    async with _folder_lock:  # one Attachments folder per edit, however many uploads start at once
        folder = ea.folder_of(edit_id)
        if not folder:
            folder = await gdrive.create_folder("Attachments", parent)
            ea.set_folder(edit_id, folder)
    _ensure_edit_open(edit_id)  # again after the awaits, right before the insert
    aid = ea.create(edit_id, author, name, size, mime)
    try:
        url = await gdrive.start_resumable_upload(name, folder, size, mime, {"attachment_id": str(aid)},
                                                  config.get("public_base_url") or None)
    except BaseException:
        ea.forget(aid)
        raise
    return {"id": aid, "upload_url": url}


async def complete_attachment_upload(aid, author, file_id) -> dict:
    """Checks Drive's file is this upload (folder, id, size, type) before it can be attached."""
    a = ea.get(aid)
    if a is None or a["status"] == "deleted":
        raise ValueError("No such attachment")
    if a["author"] != author:
        raise PermissionError("Only the uploader can finish this")
    if a["status"] == "ready":
        return a
    f = await gdrive.get_file(file_id)
    if (ea.folder_of(a["edit_id"]) not in f.get("parents", [])
            or f.get("appProperties", {}).get("attachment_id") != str(aid)
            or f.get("size") != a["size"] or f.get("mimeType") != a["mime"]):
        # Only this upload's own file is deleted (its wrong type or size slipped past the browser); any other
        # id is refused untouched, or a crafted /complete could delete a VOD outside the two-step delete.
        if f.get("appProperties", {}).get("attachment_id") == str(aid):
            await gdrive.delete_file(file_id)
        ea.set_status(aid, "deleted")
        raise ValueError("That file is not this attachment's upload")
    deletions.ensure_edit_not_deleting(a["edit_id"])
    ea.set_status(aid, "ready", file_id)
    return ea.get(aid)


async def attachment_media_link(aid) -> dict:
    a = ea.get(aid)
    if a is None or a["status"] != "ready":
        raise Unavailable(404, "This image is not available")
    # the same drive.file token as media_link, so the same accepted ceiling
    return {"url": f"{gdrive.API}/files/{a['drive_file_id']}?alt=media", "access_token": await gdrive.access_token(),
            "expires_in": max(0, int(gdrive.token_expires_at() - time.time()))}


async def vod_ready(project_id, file_id, actor) -> dict:
    p = es.get_project(project_id)
    if p is None:
        raise ValueError("No such project")
    if p["vod_status"] in ("deleting", "deleted"):
        raise ValueError("This VOD is being deleted")
    if p["vod_status"] == "ready":
        return p
    deletions.ensure_project_not_deleting(project_id)
    f = await gdrive.get_file(file_id)
    if (not p["drive_folder_id"] or p["drive_folder_id"] not in f.get("parents", [])
            or f.get("appProperties", {}).get("project_id") != str(project_id)
            or f.get("size") != p["vod_size"]):
        raise ValueError("That file is not this project's upload")
    email = config.get("editor_google_email", "")
    if email:
        # Best effort, like complete_iteration_upload: a raise here left the VOD never ready, the PC retrying forever.
        try:
            await gdrive.share_with(file_id, email, "reader")
        except Exception as e:
            log.warning(f"could not share the VOD of project {project_id} with the editor: {e}")
    deletions.ensure_project_not_deleting(project_id)
    with db.transaction():
        p = es.mark_vod_ready(project_id, file_id)
        es.record_event(actor, "vod_ready", f"{p['name']} VOD is ready", project_id=project_id)
    return p


async def media_link(iteration_id: "int | None" = None, project_id: "int | None" = None) -> dict:
    if iteration_id is not None:
        it = es.get_iteration(iteration_id)
        file_id, status = (it["drive_file_id"], it["status"]) if it else (None, None)
    else:
        p = es.get_project(project_id) if project_id is not None else None
        file_id, status = (p["vod_file_id"], p["vod_status"]) if p else (None, None)
    if not file_id:
        raise ValueError("No file to open yet")
    if status not in ("ready", "deleting"):  # deleted (or never finished): Drive would only answer 404
        raise Unavailable(409, "This file is not available")
    token = await gdrive.access_token()
    try:
        f = await gdrive.get_file(file_id)
    except gdrive.DriveError as e:
        if e.status == 404:
            raise Unavailable(404, "This file is no longer on Drive") from e
        raise
    # ponytail: a raw drive.file token reaches every file this app made, not just this one;
    # both users can already open them all. Proxy or per-file links if a third user ever joins.
    return {"url": f"{gdrive.API}/files/{file_id}?alt=media", "access_token": token,
            "expires_in": max(0, int(gdrive.token_expires_at() - time.time())),
            "drive_url": f.get("webViewLink")}


async def storage_view() -> dict:
    files = []
    for p in es.list_projects():
        if p["vod_status"] in ("ready", "deleting"):
            files.append({"kind": "vod", "label": f"{p['name']} — VOD", "size": p["vod_size"],
                          "project_id": p["id"], "edit_id": None, "status": p["vod_status"],
                          "project_name": p["name"], "vod_filename": p["vod_filename"], "edit_name": None})
        for t in es.list_edits(p["id"]):
            for i in es.list_iterations(t["id"]):
                if i["status"] in ("ready", "deleting"):
                    files.append({"kind": "iteration", "label": f"{t['name']} v{i['version']}",
                                  "size": i["size"], "project_id": p["id"], "edit_id": t["id"],
                                  "status": i["status"], "project_name": p["name"],
                                  "vod_filename": None, "edit_name": t["name"]})
    return {"quota": await free_space(), "files": files, "pending_deletes": deletions.pending()}
