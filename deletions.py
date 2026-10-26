"""
Two-step deletes with a one-hour undo, executed against Google Drive.

Nothing is deleted without typing the exact name, and every delete waits
DELETE_GRACE_SECONDS with Undo. The Mac Mini loses power often, so the boot
calls apply_boot_grace() to give every pending delete a full hour again, and
each Drive call is bracketed by its own short transactions: mark the file row
'deleting', commit, await Drive outside any transaction, then record the
outcome. A row left 'deleting' by a power cut is retried; gdrive.delete_file
treats a 404 as success, so the retry is idempotent. Deletes are permanent
(Drive DELETE), never Trash.
"""
import logging
import time

import db
import editor_store as es
import gdrive
import scheduler

log = logging.getLogger(__name__)

DELETE_GRACE_SECONDS = 3600

_DDL = """
CREATE TABLE IF NOT EXISTS delete_requests(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('vod','tape_files','project')),
  target_id INTEGER NOT NULL, label TEXT NOT NULL, requested_by TEXT NOT NULL,
  requested_at REAL NOT NULL, delete_at REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','cancelled')));
CREATE TABLE IF NOT EXISTS delete_files(
  id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES delete_requests(id),
  drive_file_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','deleting','deleted')),
  prev_status TEXT,
  target_kind TEXT NOT NULL CHECK (target_kind IN ('vod','iteration','folder')),
  target_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL,
  last_error TEXT);
"""


class ConfirmMismatch(ValueError):
    pass


class NotAllowed(PermissionError):
    pass


def _c():
    # Both DDLs before any transaction: executescript COMMITs, which would end one early.
    es._c()
    db.ensure(_DDL)
    return db.get()


def _now(now):
    return time.time() if now is None else now


def _target(kind, target_id):
    """(label, project_id, tape_id, files) where files = [(drive_id, target_kind, target_id, prev_status)]."""
    if kind == "vod":
        p = es.get_project(target_id)
        if p is None or p["deleted_at"] is not None:
            raise ValueError("No such project")
        if p["vod_status"] == "deleted":
            raise ValueError("The VOD is already deleted")
        if not p["vod_file_id"]:
            raise ValueError("The VOD has no file to delete")
        return p["vod_filename"], p["id"], None, [(p["vod_file_id"], "vod", p["id"], p["vod_status"])]
    if kind == "tape_files":
        t = es.get_tape(target_id)
        if t is None:
            raise ValueError("No such tape")
        files = [(i["drive_file_id"], "iteration", i["id"], i["status"])
                 for i in es.list_iterations(t["id"]) if i["drive_file_id"] and i["status"] != "deleted"]
        return t["name"], t["project_id"], t["id"], files
    if kind == "project":
        p = es.get_project(target_id)
        if p is None or p["deleted_at"] is not None:
            raise ValueError("No such project")
        files = [(p["drive_folder_id"], "folder", p["id"], None)] if p["drive_folder_id"] else []
        return p["name"], p["id"], None, files
    raise ValueError(f"unknown kind {kind!r}")


def _check_allowed(kind, target_id, role):
    if role == "admin":
        return
    if role == "editor" and kind == "vod":
        p = es.get_project(target_id)
        if p is not None and p["download_clicked_at"] is not None:
            return
    raise NotAllowed("Only the streamer can do this.")


def request_delete(kind, target_id, confirm: str, *, username: str, role: str, now=None) -> dict:
    now = _now(now)
    conn = _c()
    _check_allowed(kind, target_id, role)
    delete_at = now + DELETE_GRACE_SECONDS
    with db.transaction():
        label, project_id, tape_id, files = _target(kind, target_id)
        if (confirm or "").strip() != label:
            raise ConfirmMismatch("Type the name exactly to confirm")
        if conn.execute("SELECT 1 FROM delete_requests WHERE kind=? AND target_id=?"
                        " AND status IN ('pending','running')", (kind, target_id)).fetchone():
            raise ValueError("Already scheduled for deletion")
        rid = conn.execute(
            "INSERT INTO delete_requests(kind, target_id, label, requested_by, requested_at, delete_at, status)"
            " VALUES(?,?,?,?,?,?,'pending')", (kind, target_id, label, username, now, delete_at)).lastrowid
        for drive_id, tkind, tid, prev in files:
            conn.execute("INSERT INTO delete_files(request_id, drive_file_id, status, prev_status, target_kind,"
                         " target_id, next_attempt_at) VALUES(?,?,'pending',?,?,?,?)",
                         (rid, drive_id, prev, tkind, tid, delete_at))
            if tkind == "vod":
                conn.execute("UPDATE projects SET vod_status='deleting', updated_at=? WHERE id=?", (now, tid))
            elif tkind == "iteration":
                conn.execute("UPDATE iterations SET status='deleting', updated_at=? WHERE id=?", (now, tid))
        es.record_event(username, "delete_scheduled", f"{username} scheduled deletion of {label}",
                        project_id=project_id, tape_id=tape_id, now=now)
    return {"request_id": rid, "delete_at": delete_at, "label": label}


def _event_ids(req):
    """(project_id, tape_id) for a request's events."""
    if req["kind"] == "tape_files":
        t = es.get_tape(req["target_id"])
        return (t["project_id"] if t else None), req["target_id"]
    return req["target_id"], None


def undo(request_id, *, username, role, now=None) -> dict:
    now = _now(now)
    conn = _c()
    with db.transaction():
        req = conn.execute("SELECT * FROM delete_requests WHERE id=?", (request_id,)).fetchone()
        if req is None:
            raise ValueError("No such delete request")
        if role != "admin" and req["requested_by"] != username:
            raise NotAllowed("Only the streamer can undo someone else's delete.")
        if req["status"] != "pending":
            raise ValueError("Too late to undo: the delete has started")
        conn.execute("UPDATE delete_requests SET status='cancelled' WHERE id=?", (request_id,))
        for f in conn.execute("SELECT * FROM delete_files WHERE request_id=?", (request_id,)).fetchall():
            # Only rows still 'deleting': something else (e.g. a project delete) may have finished them.
            if f["target_kind"] == "vod":
                conn.execute("UPDATE projects SET vod_status=?, updated_at=? WHERE id=? AND vod_status='deleting'",
                             (f["prev_status"], now, f["target_id"]))
            elif f["target_kind"] == "iteration":
                conn.execute("UPDATE iterations SET status=?, updated_at=? WHERE id=? AND status='deleting'",
                             (f["prev_status"], now, f["target_id"]))
        project_id, tape_id = _event_ids(req)
        es.record_event(username, "delete_undone", f"{username} undid deletion of {req['label']}",
                        project_id=project_id, tape_id=tape_id, now=now)
    return {"request_id": request_id, "status": "cancelled"}


def pending(now=None) -> list[dict]:
    now = _now(now)
    rows = _c().execute("SELECT * FROM delete_requests WHERE status IN ('pending','running')"
                        " ORDER BY delete_at, id").fetchall()
    return [{**dict(r), "remaining_seconds": max(0.0, r["delete_at"] - now)} for r in rows]


def apply_boot_grace(boot: float) -> int:
    conn = _c()
    with db.transaction():
        touched = conn.execute("UPDATE delete_requests SET delete_at=? WHERE status='pending' AND delete_at<?",
                               (boot + DELETE_GRACE_SECONDS, boot + DELETE_GRACE_SECONDS)).rowcount
        conn.execute("UPDATE delete_files SET status='pending', next_attempt_at=? WHERE status='deleting'", (boot,))
    return touched


async def _delete_one(f, now):
    conn = db.get()
    with db.transaction():
        claimed = conn.execute("UPDATE delete_files SET status='deleting' WHERE id=? AND status='pending'",
                               (f["id"],)).rowcount
    if not claimed:
        return
    try:
        await gdrive.delete_file(f["drive_file_id"])  # outside any transaction
    except Exception as e:
        if not isinstance(e, (gdrive.DriveNotConnected, gdrive.DriveError)):
            log.exception("Unexpected error deleting Drive file %s", f["drive_file_id"])
        with db.transaction():
            conn.execute("UPDATE delete_files SET status='pending', attempts=attempts+1, next_attempt_at=?,"
                         " last_error=? WHERE id=?",
                         (now + scheduler.backoff_seconds(f["attempts"]), f"{type(e).__name__}: {e}", f["id"]))
        return
    with db.transaction():
        conn.execute("UPDATE delete_files SET status='deleted', last_error=NULL WHERE id=?", (f["id"],))


def _finalize(req, now):
    conn = db.get()
    with db.transaction():
        if conn.execute("SELECT 1 FROM delete_files WHERE request_id=? AND status<>'deleted'",
                        (req["id"],)).fetchone():
            return
        kind, tid = req["kind"], req["target_id"]
        if kind == "vod":
            conn.execute("UPDATE projects SET vod_status='deleted', updated_at=? WHERE id=?", (now, tid))
        elif kind == "tape_files":
            conn.execute("UPDATE iterations SET status='deleted', updated_at=? WHERE id IN"
                         " (SELECT target_id FROM delete_files WHERE request_id=? AND target_kind='iteration')",
                         (now, req["id"]))
            conn.execute("UPDATE tapes SET files_deleted_at=?, updated_at=? WHERE id=?", (now, now, tid))
        else:
            # The folder took its children with it; mark them so nothing lists files that are gone.
            conn.execute("UPDATE projects SET deleted_at=?, vod_status='deleted', updated_at=? WHERE id=?",
                         (now, now, tid))
            conn.execute("UPDATE iterations SET status='deleted', updated_at=? WHERE status<>'deleted'"
                         " AND tape_id IN (SELECT id FROM tapes WHERE project_id=?)", (now, tid))
            conn.execute("UPDATE tapes SET files_deleted_at=COALESCE(files_deleted_at, ?), updated_at=?"
                         " WHERE project_id=?", (now, now, tid))
        conn.execute("UPDATE delete_requests SET status='done' WHERE id=?", (req["id"],))
        project_id, tape_id = _event_ids(req)
        es.record_event("system", "delete_done", f"Deleted {req['label']}",
                        project_id=project_id, tape_id=tape_id, now=now)


async def run_due(now: float) -> None:
    """Scheduler job. Never raises: every failure is logged and retried on a later tick."""
    try:
        conn = _c()
        with db.transaction():
            conn.execute("UPDATE delete_requests SET status='running' WHERE status='pending' AND delete_at<=?",
                         (now,))
        files = conn.execute(
            "SELECT f.* FROM delete_files f JOIN delete_requests r ON r.id=f.request_id"
            " WHERE r.status='running' AND f.status='pending' AND f.next_attempt_at<=? ORDER BY f.id",
            (now,)).fetchall()
        requests = conn.execute("SELECT * FROM delete_requests WHERE status='running' ORDER BY id").fetchall()
    except Exception:
        log.exception("Could not read due deletions")
        return
    for f in files:
        try:
            await _delete_one(f, now)
        except Exception:
            log.exception("Delete file row %s failed", f["id"])
    for req in requests:
        try:
            _finalize(req, now)
        except Exception:
            log.exception("Finalizing delete request %s failed", req["id"])
