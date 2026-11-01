"""No-login share links for one version, and the notes guests leave on them.

A live link is an "anyone with the link" reader permission on that one Drive file, so the guest's
browser plays it straight from Google with the public API key; the drive.file token never leaves
the backend. The link secret travels in the URL fragment (never logged, never in Referer) and only
its sha256 is stored. Expiry, Revoke and an untrusted clock all remove the permission; until Drive
confirms that, the link sits in 'revoking' and the guest API already refuses it.

Guest notes wait for the streamer: approved ones become ordinary comments by "guest:<name>", which
is the only way the editor ever sees them.
"""
import hashlib
import json
import secrets
import time

from core import db, scheduler
from core.config import config
from core.logger import get_logger
from editor import deletions
from editor import editor_comments as ec
from editor import editor_store as es
from editor import gdrive
from editor.editor_store import _all, _one

log = get_logger("ShareLinks")

HOURS = (1, 2, 10, 24, 168)
DEFAULT_HOURS = 2
NAME_MAX = 40
BODY_MAX = 2000
BURST = 30  # notes per link per BURST_SECONDS
BURST_SECONDS = 600
TOTAL_MAX = 200  # notes per link, ever
GONE = "This link has expired or was revoked."

_DDL = """
CREATE TABLE IF NOT EXISTS share_links(
  id INTEGER PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE, iteration_id INTEGER NOT NULL,
  drive_file_id TEXT NOT NULL, permission_id TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at REAL NOT NULL, expires_at REAL NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('live','revoking','revoked')),
  revoke_reason TEXT, revoked_at REAL, attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS guest_notes(
  id INTEGER PRIMARY KEY, link_id INTEGER NOT NULL REFERENCES share_links(id), iteration_id INTEGER NOT NULL,
  name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('fix','keep','question')),
  start_s REAL, end_s REAL, body TEXT NOT NULL, client_id TEXT NOT NULL, created_at REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected')), comment_id INTEGER,
  UNIQUE (link_id, client_id));
"""
_recent: dict[int, list[float]] = {}  # link id -> note times inside the burst window


def _c():
    es._c()
    db.ensure(_DDL)
    return db.get()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def create(iteration_id, hours, by, now=None) -> dict:
    now = time.time() if now is None else now
    if hours not in HOURS:
        raise ValueError("Pick 1, 2, 10 or 24 hours, or 7 days")
    if not config.get("gdrive_public_api_key"):
        raise ValueError("Add the Drive public API key in Settings first")
    it = es.get_iteration(iteration_id)
    if it is None or it["status"] != "ready" or not it["drive_file_id"]:
        raise ValueError("Only an uploaded version can be shared")
    tape = es.get_tape(it["tape_id"])
    deletions.ensure_project_not_deleting(tape["project_id"])
    deletions.ensure_tape_not_deleting(tape["id"])
    conn = _c()
    token = secrets.token_urlsafe(32)
    permission = await gdrive.share_anyone(it["drive_file_id"])
    try:
        with db.transaction():
            lid = conn.execute(
                "INSERT INTO share_links(token_hash, iteration_id, drive_file_id, permission_id, created_by,"
                " created_at, expires_at, state) VALUES(?,?,?,?,?,?,?,'live')",
                (_hash(token), iteration_id, it["drive_file_id"], permission, by, now, now + hours * 3600)).lastrowid
    except BaseException:
        await gdrive.unshare(it["drive_file_id"], permission)  # never a public file with no row to revoke it
        raise
    base = (config.get("public_base_url") or "").rstrip("/")
    # The token is returned once, here, and never again.
    return {"id": lid, "url": f"{base}/s#{token}", "expires_at": now + hours * 3600}


def find(token, now=None) -> "dict | None":
    """The live link for a guest's token, or None for every kind of failure alike."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or not 20 <= len(token) <= 100 or not scheduler.clock_is_sane(now):
        return None
    _c()
    link = _one("SELECT * FROM share_links WHERE token_hash=? AND state='live' AND expires_at>?", (_hash(token), now))
    if link is None:
        return None
    it = es.get_iteration(link["iteration_id"])  # the file was deleted: nothing left to share
    return link if it is not None and it["status"] == "ready" and it["drive_file_id"] == link["drive_file_id"] else None


async def _revoke(link, reason, now) -> bool:
    conn = _c()
    with db.transaction():
        conn.execute("UPDATE share_links SET state='revoking', revoke_reason=COALESCE(revoke_reason, ?)"
                     " WHERE id=? AND state IN ('live','revoking')", (reason, link["id"]))
    try:
        await gdrive.unshare(link["drive_file_id"], link["permission_id"])  # a 404 is success
    except (gdrive.DriveError, gdrive.DriveNotConnected) as e:
        with db.transaction():
            conn.execute("UPDATE share_links SET attempts=attempts+1, next_try=? WHERE id=?",
                         (now + scheduler.backoff_seconds(link["attempts"]), link["id"]))
        log.warning("Could not revoke share link %s yet: %s", link["id"], type(e).__name__)
        return False
    with db.transaction():
        conn.execute("UPDATE share_links SET state='revoked', revoked_at=? WHERE id=?", (now, link["id"]))
    return True


async def revoke(link_id, now=None) -> bool:
    now = time.time() if now is None else now
    _c()
    link = _one("SELECT * FROM share_links WHERE id=?", (link_id,))
    if link is None or link["state"] == "revoked":
        raise ValueError("No such live link")
    return await _revoke(link, "revoked", now)


async def run(now) -> None:
    """Scheduler job: expired links, and revokes Drive refused earlier."""
    _c()
    for link in _all("SELECT * FROM share_links WHERE (state='live' AND expires_at<=?)"
                     " OR (state='revoking' AND next_try<=?) ORDER BY id", (now, now)):
        await _revoke(link, "expired", now)


async def revoke_all_live(now) -> None:
    """The clock cannot be trusted, so no expiry can be: revoke every link now (early is the safe side)."""
    _c()
    for link in _all("SELECT * FROM share_links WHERE state IN ('live','revoking') ORDER BY id"):
        await _revoke(link, "clock", now)


def list_live(now=None) -> list:
    now = time.time() if now is None else now
    _c()
    rows = _all("SELECT l.id, l.iteration_id, l.created_at, l.expires_at, i.version, t.id AS tape_id, t.name AS tape,"
                " t.project_id FROM share_links l JOIN iterations i ON i.id=l.iteration_id JOIN tapes t ON t.id=i.tape_id"
                " WHERE l.state='live' AND l.expires_at>? ORDER BY l.expires_at", (now,))
    return rows


def view(link) -> dict:
    """What a guest's page needs, and nothing more (no project names, no comments)."""
    it = es.get_iteration(link["iteration_id"])
    tape = es.get_tape(it["tape_id"])
    try:
        checks = json.loads(it["checks_json"] or "null") or {}
    except ValueError:
        checks = {}
    return {"title": f"{tape['name']} v{it['version']}", "file_id": link["drive_file_id"],
            "api_key": config.get("gdrive_public_api_key", ""), "expires_at": link["expires_at"],
            "duration_s": checks.get("duration_s") if isinstance(checks, dict) else None,
            "codec": checks.get("codec") if isinstance(checks, dict) else None,
            "playable": checks.get("playable") if isinstance(checks, dict) else None}


def add_note(link, name, kind, body, start_s, end_s, client_id, now=None) -> dict:
    now = time.time() if now is None else now
    name = name.strip() if isinstance(name, str) else ""
    body = body.strip() if isinstance(body, str) else ""
    if not 1 <= len(name) <= NAME_MAX:
        raise ValueError(f"Type a name of up to {NAME_MAX} characters")
    if not 1 <= len(body) <= BODY_MAX:
        raise ValueError(f"Write a note of up to {BODY_MAX} characters")
    if kind not in ec.KINDS:
        raise ValueError("kind must be one of fix, keep, question")
    if end_s is not None and (start_s is None or end_s <= start_s):
        raise ValueError("end_s needs a start_s and must be after it")
    if start_s is not None and start_s < 0:
        raise ValueError("start_s must be >= 0")
    conn = _c()
    old = _one("SELECT * FROM guest_notes WHERE link_id=? AND client_id=?", (link["id"], client_id))
    if old:
        return old
    times = [t for t in _recent.get(link["id"], []) if now - t < BURST_SECONDS]
    total = conn.execute("SELECT COUNT(*) FROM guest_notes WHERE link_id=?", (link["id"],)).fetchone()[0]
    if len(times) >= BURST or total >= TOTAL_MAX:
        raise OverflowError("Slow down a little")
    it = es.get_iteration(link["iteration_id"])
    tape = es.get_tape(it["tape_id"])
    with db.transaction():
        nid = conn.execute(
            "INSERT INTO guest_notes(link_id, iteration_id, name, kind, start_s, end_s, body, client_id, created_at,"
            " status) VALUES(?,?,?,?,?,?,?,?,?,'pending')",
            (link["id"], link["iteration_id"], name, kind, start_s, end_s, body, client_id, now)).lastrowid
        es.record_event(f"guest:{name}", "guest_notes_added", f"{name} left a guest note",
                        project_id=tape["project_id"], tape_id=tape["id"], iteration_id=it["id"], now=now)
    _recent[link["id"]] = times + [now]
    return _one("SELECT * FROM guest_notes WHERE id=?", (nid,))


def pending(iteration_id) -> list:
    """Pending notes on this version, grouped under each guest's name, oldest first."""
    _c()
    groups: dict[str, list] = {}
    for n in _all("SELECT id, name, kind, start_s, end_s, body, created_at FROM guest_notes"
                  " WHERE iteration_id=? AND status='pending' ORDER BY created_at, id", (iteration_id,)):
        groups.setdefault(n["name"], []).append(n)
    return [{"name": k, "notes": v} for k, v in groups.items()]


def approve(ids, by, now=None) -> int:
    """Each still-pending note becomes a comment by guest:<name>; the event is the streamer's, so the
    editor hears of it as he hears of any streamer comment. A retry is idempotent by client_id."""
    _c()
    done = 0
    for n in _notes(ids):
        if n["status"] == "rejected":
            continue
        c = ec.add_comment(n["iteration_id"], f"guest:{n['name']}", n["kind"], n["body"], n["start_s"], n["end_s"],
                           now=now, client_id=f"guest-note-{n['id']}", actor=by)
        with db.transaction() as conn:
            conn.execute("UPDATE guest_notes SET status='approved', comment_id=? WHERE id=?", (c["id"], n["id"]))
        done += 1
    return done


def reject(ids) -> int:
    _c()
    marks = [n["id"] for n in _notes(ids) if n["status"] == "pending"]
    with db.transaction() as conn:
        for i in marks:
            conn.execute("UPDATE guest_notes SET status='rejected' WHERE id=? AND status='pending'", (i,))
    return len(marks)


def _notes(ids) -> list:
    if not isinstance(ids, list) or not ids or len(ids) > 500 or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ValueError("ids must be a list of note ids")
    return _all(f"SELECT * FROM guest_notes WHERE id IN ({','.join('?' * len(ids))}) ORDER BY created_at, id", tuple(ids))
