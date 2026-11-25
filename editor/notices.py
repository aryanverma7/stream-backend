"""The notice board: short plain-text notices from the streamer, pinned at the top of the editor's Home.

The streamer creates, edits and deletes them; the editor's "Got it" records seen_at (the first time only).
A new notice is a notice_posted event (the editor's bell) and a DM to the editor, queued in the same
transaction (dedupe notice:<id>, the text included; discord_dm truncates past 2000). Edits do not re-notify,
but clear seen_at. A delete drops its unsent DM and blanks its bell summary.
"""
import time

from core import db
from editor import editor_store as es
from editor import notify_outbox

MAX_CHARS = 2000

_DDL = """
CREATE TABLE IF NOT EXISTS notices(
  id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, created_by TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, seen_at REAL, event_id INTEGER);
"""


def _c():
    es._c()
    notify_outbox.ensure_schema()  # create() enqueues inside its transaction
    db.ensure(_DDL)
    return db.get()


def clean(text) -> str:
    """ValueError unless it is non-empty text of at most MAX_CHARS."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("A notice needs some text")
    text = text.strip()
    if len(text) > MAX_CHARS:
        raise ValueError(f"A notice can be at most {MAX_CHARS} characters")
    return text


def get(id) -> "dict | None":
    row = _c().execute("SELECT * FROM notices WHERE id=?", (id,)).fetchone()
    return dict(row) if row else None


def list_all() -> list:
    return [dict(r) for r in _c().execute("SELECT * FROM notices ORDER BY created_at DESC, id DESC")]


def create(text, actor, now=None) -> dict:
    text = clean(text)
    now = time.time() if now is None else now
    conn = _c()
    with db.transaction():
        nid = conn.execute("INSERT INTO notices(text, created_by, created_at, updated_at) VALUES(?,?,?,?)",
                           (text, actor, now, now)).lastrowid
        eid = es.record_event(actor, "notice_posted", f"{actor} posted a notice: {text[:200]}", now=now)
        conn.execute("UPDATE notices SET event_id=? WHERE id=?", (eid, nid))
        notify_outbox.enqueue("editor", f"New notice from {actor}: {text}", f"notice:{nid}", now)
    return get(nid)


def update(id, text, now=None) -> "dict | None":
    text = clean(text)
    now = time.time() if now is None else now
    # changed text is a new thing to read: the editor gets "Got it" again
    _c().execute("UPDATE notices SET text=?, updated_at=?, seen_at=NULL WHERE id=?", (text, now, id))
    return get(id)


def delete(id) -> bool:
    """Also drops its DM if not sent yet and blanks its bell summary, so the deleted text goes nowhere."""
    conn = _c()
    with db.transaction():
        row = conn.execute("SELECT event_id FROM notices WHERE id=?", (id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM notices WHERE id=?", (id,))
        conn.execute("UPDATE dm_outbox SET status='dropped', last_error='notice deleted'"
                     " WHERE dedupe_key=? AND status='pending'", (f"notice:{id}",))
        conn.execute("UPDATE events SET summary='' WHERE id=?", (row["event_id"],))
    return True


def mark_seen(id, now=None) -> "dict | None":
    now = time.time() if now is None else now
    _c().execute("UPDATE notices SET seen_at=? WHERE id=? AND seen_at IS NULL", (now, id))
    return get(id)
