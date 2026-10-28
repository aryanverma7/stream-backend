"""Per-user editor data: private to-dos, what each person has seen per tape, and playback position.

Every read is keyed by the caller's username, so another user's row is simply not found (None)."""
import time

from core import db
from editor import editor_store as es

_DDL = """
CREATE TABLE IF NOT EXISTS todos(
  id INTEGER PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tape_seen(
  username TEXT, tape_id INTEGER, seen_at REAL, PRIMARY KEY (username, tape_id));
CREATE TABLE IF NOT EXISTS playback(
  username TEXT, iteration_id INTEGER, position_s REAL, updated_at REAL, PRIMARY KEY (username, iteration_id));
"""


def _c():
    es._c()  # events table, read by tape_has_new
    db.ensure(_DDL)
    return db.get()


def _now(now):
    return time.time() if now is None else now


def _todo(row):
    return None if row is None else {**dict(row), "done": bool(row["done"])}


def _clean(text):
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        raise ValueError("A to-do needs some text")
    return text


# -- to-dos --

def list_todos(owner):
    return [_todo(r) for r in _c().execute("SELECT * FROM todos WHERE owner=? ORDER BY done, id", (owner,))]


def get_todo(owner, id):
    return _todo(_c().execute("SELECT * FROM todos WHERE id=? AND owner=?", (id, owner)).fetchone())


def add_todo(owner, text, now=None):
    now = _now(now)
    cur = _c().execute("INSERT INTO todos(owner, text, created_at, updated_at) VALUES(?,?,?,?)",
                       (owner, _clean(text), now, now))
    return get_todo(owner, cur.lastrowid)


def update_todo(owner, id, *, text=None, done=None, now=None):
    if get_todo(owner, id) is None:
        return None
    cols = {"updated_at": _now(now)}
    if text is not None:
        cols["text"] = _clean(text)
    if done is not None:
        cols["done"] = 1 if done else 0
    sets = ", ".join(f"{k}=?" for k in cols)
    _c().execute(f"UPDATE todos SET {sets} WHERE id=? AND owner=?", (*cols.values(), id, owner))
    return get_todo(owner, id)


def delete_todo(owner, id) -> bool:
    return _c().execute("DELETE FROM todos WHERE id=? AND owner=?", (id, owner)).rowcount > 0


# -- "new" per tape --

def mark_tape_seen(username, tape_id, now=None):
    _c().execute("INSERT INTO tape_seen(username, tape_id, seen_at) VALUES(?,?,?)"
                 " ON CONFLICT(username, tape_id) DO UPDATE SET seen_at=excluded.seen_at",
                 (username, tape_id, _now(now)))


def tape_has_new(username, tape_id) -> bool:
    """True when someone else did something on the tape after this user last opened it."""
    return _c().execute(
        "SELECT 1 FROM events e WHERE e.tape_id=? AND e.actor<>?"
        " AND e.at > COALESCE((SELECT seen_at FROM tape_seen WHERE username=? AND tape_id=?), -1e300)"
        " LIMIT 1", (tape_id, username, username, tape_id)).fetchone() is not None


# -- playback position --

def set_position(username, iteration_id, position_s, now=None):
    _c().execute("INSERT INTO playback(username, iteration_id, position_s, updated_at) VALUES(?,?,?,?)"
                 " ON CONFLICT(username, iteration_id) DO UPDATE SET position_s=excluded.position_s,"
                 " updated_at=excluded.updated_at", (username, iteration_id, float(position_s), _now(now)))


def get_position(username, iteration_id) -> float:
    row = _c().execute("SELECT position_s FROM playback WHERE username=? AND iteration_id=?",
                       (username, iteration_id)).fetchone()
    return 0.0 if row is None else float(row["position_s"])
