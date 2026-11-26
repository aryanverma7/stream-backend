"""
The transcript queue: review versions (iterations, never VODs) waiting for the gaming PC to transcribe
them into romanized Hinglish, and the lines it sent back.

The PC claims one job at a time with a fresh random token; complete/release only count with the token
of the current claim, so a PC that lost its job (re-claimed after CLAIM_TTL_SECONDS, e.g. a power cut
mid-job) cannot overwrite the new claim. A row whose version is no longer `ready` is dropped on claim,
and deletions.forget()s rows once a version's file is deleted.

Two switches live in `meta`: transcriber_enabled (the PC may work; default Paused) and transcribe_auto
(queue every new version; default off).
"""
import json
import math
import secrets
import time

from core import db
from editor import editor_store as es

CLAIM_TTL_SECONDS = 1800  # a running claim older than this is a dead PC: back to the queue
MAX_ATTEMPTS = 3  # failed releases (with an error) before the job is `failed`
MAX_SEGMENTS = 20000
MAX_TEXT = 2000

_DDL = """
CREATE TABLE IF NOT EXISTS transcripts(
  iteration_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed')),
  requested_by TEXT, queued_at REAL, claimed_at REAL, claim_token TEXT,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, segments TEXT, model TEXT, updated_at REAL);
"""


def _c():
    es._c()
    db.ensure(_DDL)
    return db.get()


def _now(now):
    return time.time() if now is None else now


def settings() -> dict:
    return {"enabled": db.meta_get("transcriber_enabled") == "1", "auto": db.meta_get("transcribe_auto") == "1"}


def set_settings(enabled: "bool | None" = None, auto: "bool | None" = None) -> dict:
    if enabled is not None:
        db.meta_set("transcriber_enabled", "1" if enabled else "0")
    if auto is not None:
        db.meta_set("transcribe_auto", "1" if auto else "0")
    return settings()


def get(iteration_id) -> "dict | None":
    row = _c().execute("SELECT * FROM transcripts WHERE iteration_id=?", (iteration_id,)).fetchone()
    if row is None:
        return None
    row = dict(row)
    row["segments"] = json.loads(row["segments"]) if row["segments"] else None
    return row


def request(iteration_id, actor, now=None) -> dict:
    """Queue a version. Idempotent: a queued/running/done row comes back unchanged; failed is retried."""
    now = _now(now)
    conn = _c()
    with db.transaction():
        it = conn.execute("SELECT status FROM iterations WHERE id=?", (iteration_id,)).fetchone()
        if it is None or it["status"] != "ready":
            raise ValueError("This version is not ready")
        conn.execute("INSERT INTO transcripts(iteration_id, status, requested_by, queued_at, updated_at)"
                     " VALUES(?,'queued',?,?,?) ON CONFLICT(iteration_id) DO UPDATE SET status='queued',"
                     " attempts=0, error=NULL, queued_at=excluded.queued_at, updated_at=excluded.updated_at"
                     " WHERE status='failed'", (iteration_id, actor, now, now))
    return get(iteration_id)


def on_iteration_ready(iteration_id) -> None:
    if settings()["auto"]:
        request(iteration_id, "system")


def claim(now=None) -> "dict | None":
    """The oldest queued job for a still-ready version, now running under a new token."""
    now = _now(now)
    conn = _c()
    with db.transaction():
        conn.execute("UPDATE transcripts SET status='queued', claim_token=NULL, updated_at=?"
                     " WHERE status='running' AND claimed_at<?", (now, now - CLAIM_TTL_SECONDS))
        # Only rows whose version is gone entirely; a version in its delete grace hour can still be undone,
        # so its transcript (done or queued) waits, and is simply not handed out while it is not ready.
        conn.execute("DELETE FROM transcripts WHERE iteration_id NOT IN (SELECT id FROM iterations)")
        row = conn.execute("SELECT t.iteration_id FROM transcripts t JOIN iterations i ON i.id=t.iteration_id"
                           " WHERE t.status='queued' AND i.status='ready'"
                           " ORDER BY t.queued_at, t.iteration_id LIMIT 1").fetchone()
        if row is None:
            return None
        token = secrets.token_hex(16)
        conn.execute("UPDATE transcripts SET status='running', claimed_at=?, claim_token=?, updated_at=?"
                     " WHERE iteration_id=?", (now, token, now, row["iteration_id"]))
    return {"iteration_id": row["iteration_id"], "token": token}


def _running(conn, iteration_id, token) -> bool:
    row = conn.execute("SELECT claim_token FROM transcripts WHERE iteration_id=? AND status='running'",
                       (iteration_id,)).fetchone()
    return (row is not None and isinstance(token, str) and row["claim_token"] is not None
            and secrets.compare_digest(row["claim_token"], token))


def release(iteration_id, token, error: "str | None", now=None) -> bool:
    """Back to the queue (the PC got busy); with an error, one attempt spent, `failed` at MAX_ATTEMPTS."""
    now = _now(now)
    conn = _c()
    with db.transaction():
        if not _running(conn, iteration_id, token):
            return False
        if error is None:
            conn.execute("UPDATE transcripts SET status='queued', claim_token=NULL, updated_at=?"
                         " WHERE iteration_id=?", (now, iteration_id))
        else:
            conn.execute("UPDATE transcripts SET attempts=attempts+1, error=?, claim_token=NULL, updated_at=?,"
                         " status=CASE WHEN attempts+1>=? THEN 'failed' ELSE 'queued' END WHERE iteration_id=?",
                         (str(error)[:MAX_TEXT], now, MAX_ATTEMPTS, iteration_id))
    return True


def complete(iteration_id, token, segments, model, now=None) -> bool:
    """False when the token is not the current running claim. ValueError for bad segments."""
    data = json.dumps(validate_segments(segments), ensure_ascii=False)
    now = _now(now)
    conn = _c()
    with db.transaction():
        if not _running(conn, iteration_id, token):
            return False
        conn.execute("UPDATE transcripts SET status='done', segments=?, model=?, error=NULL, claim_token=NULL,"
                     " updated_at=? WHERE iteration_id=?", (data, str(model)[:200], now, iteration_id))
    return True


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
        raise ValueError("start and end must be numbers of seconds, 0 or more")
    return float(v)


def validate_segments(segments) -> list:
    if not isinstance(segments, list):
        raise ValueError("segments must be a list")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"At most {MAX_SEGMENTS} segments")
    out = []
    for s in segments:
        if not isinstance(s, dict) or not isinstance(s.get("text"), str):
            raise ValueError("Each segment needs start, end and text")
        start, end = _num(s.get("start")), _num(s.get("end"))
        if end < start:
            raise ValueError("A segment ends before it starts")
        if len(s["text"]) > MAX_TEXT:
            raise ValueError(f"A segment's text can be at most {MAX_TEXT} characters")
        out.append({"start": start, "end": end, "text": s["text"]})
    return out


class NotDone(Exception):
    """The transcript is not finished, so there is no line to fix."""


def edit_line(iteration_id, index, text, actor, now=None) -> dict:
    """Fix one line's text (both roles). Marks the line `edited`; a later request never overwrites it."""
    now = _now(now)
    if not isinstance(text, str) or not text.strip():
        raise ValueError("A line cannot be empty")
    text = text.strip()
    if len(text) > MAX_TEXT:
        raise ValueError(f"A line can be at most {MAX_TEXT} characters")
    conn = _c()
    with db.transaction():
        row = conn.execute("SELECT status, segments FROM transcripts WHERE iteration_id=?", (iteration_id,)).fetchone()
        if row is None or row["status"] != "done":
            raise NotDone("This transcript is not finished yet")
        segs = json.loads(row["segments"] or "[]")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(segs):
            raise ValueError("No such line")
        segs[index] = {**segs[index], "text": text, "edited": True}
        conn.execute("UPDATE transcripts SET segments=?, updated_at=? WHERE iteration_id=?",
                     (json.dumps(segs), now, iteration_id))
    return get(iteration_id)


def forget(iteration_ids) -> None:
    ids = list(iteration_ids)
    if ids:
        _c().execute(f"DELETE FROM transcripts WHERE iteration_id IN ({','.join('?' * len(ids))})", ids)


def queue_status() -> dict:
    rows = dict(_c().execute("SELECT status, COUNT(*) FROM transcripts GROUP BY status").fetchall())
    return {"queued": rows.get("queued", 0), "running": rows.get("running", 0)}


def search(query, limit=50) -> list:
    """Transcript lines containing the query: {iteration_id, edit_id, project_id, version, start, text}."""
    q = (query or "").strip().lower()
    if not q:
        return []
    # the JSON-escaped form of the query, as it sits in the stored text; lines are then matched one by one
    like = "%" + json.dumps(q, ensure_ascii=False)[1:-1].replace("\\", "\\\\").replace("%", "\\%") \
        .replace("_", "\\_") + "%"
    rows = _c().execute(
        "SELECT x.iteration_id, i.edit_id, e.project_id, i.version, x.segments FROM transcripts x"
        " JOIN iterations i ON i.id=x.iteration_id JOIN edits e ON e.id=i.edit_id"
        " JOIN projects p ON p.id=e.project_id"
        " WHERE x.status='done' AND i.status='ready' AND p.deleted_at IS NULL AND lower(x.segments) LIKE ? ESCAPE '\\'"
        " ORDER BY x.updated_at DESC, x.iteration_id DESC", (like,)).fetchall()
    hits = []
    for r in rows:
        for s in json.loads(r["segments"]):
            if q in s["text"].lower():
                hits.append({"iteration_id": r["iteration_id"], "edit_id": r["edit_id"], "project_id": r["project_id"],
                             "version": r["version"], "start": s["start"], "text": s["text"]})
                if len(hits) >= limit:
                    return hits
    return hits
