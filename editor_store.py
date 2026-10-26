"""Editor app store: projects (one per stream VOD), tapes, iterations and the events log."""
import time

import db

THUMBNAIL_MAX_BYTES = 262144
CHECKS_MAX_BYTES = 16384
TAPE_STATUSES = ("cutting", "in_review", "changes_asked", "final")

_DDL = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, stream_date TEXT NOT NULL,
  stream_title TEXT NOT NULL DEFAULT '', vod_filename TEXT NOT NULL, vod_size INTEGER,
  vod_file_id TEXT,
  vod_status TEXT NOT NULL CHECK (vod_status IN ('uploading','ready','deleting','deleted')),
  vod_upload_url TEXT, drive_folder_id TEXT, download_clicked_at REAL, deleted_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tapes(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'cutting' CHECK (status IN ('cutting','in_review','changes_asked','final')),
  notes TEXT NOT NULL DEFAULT '', drive_folder_id TEXT, files_deleted_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS iterations(
  id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL REFERENCES tapes(id),
  version INTEGER NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleting','deleted','abandoned')),
  drive_file_id TEXT, upload_url TEXT, checks_json TEXT, thumbnail BLOB, uploaded_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (tape_id, version));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  project_id INTEGER, tape_id INTEGER, iteration_id INTEGER, comment_id INTEGER,
  summary TEXT NOT NULL);
"""


def _c():
    db.ensure(_DDL)
    return db.get()


def _one(sql, args=()):
    row = _c().execute(sql, args).fetchone()
    return dict(row) if row else None


def _all(sql, args=()):
    return [dict(r) for r in _c().execute(sql, args).fetchall()]


def _set(table, id, now, **cols):
    """UPDATE the given columns plus updated_at; returns the fresh row (None if missing)."""
    cols["updated_at"] = time.time() if now is None else now
    sets = ", ".join(f"{k}=?" for k in cols)
    _c().execute(f"UPDATE {table} SET {sets} WHERE id=?", (*cols.values(), id))
    return _one(f"SELECT * FROM {table} WHERE id=?", (id,))


# -- projects --

def create_project(name, stream_date, stream_title, vod_filename, vod_size, now=None):
    now = time.time() if now is None else now
    cur = _c().execute(
        "INSERT INTO projects(name, stream_date, stream_title, vod_filename, vod_size, vod_status,"
        " created_at, updated_at) VALUES(?,?,?,?,?,'uploading',?,?)",
        (name, stream_date, stream_title, vod_filename, vod_size, now, now))
    return get_project(cur.lastrowid)


def get_project(id):
    return _one("SELECT * FROM projects WHERE id=?", (id,))


def list_projects():
    return _all("SELECT p.*, (SELECT COUNT(*) FROM tapes t WHERE t.project_id=p.id) AS tape_count"
                " FROM projects p WHERE deleted_at IS NULL ORDER BY created_at DESC, id DESC")


def rename_project(id, name, now=None):
    return _set("projects", id, now, name=name)


def set_project_folder(id, folder_id):
    return _set("projects", id, None, drive_folder_id=folder_id)


def set_vod_upload(id, upload_url):
    return _set("projects", id, None, vod_upload_url=upload_url)


def mark_vod_ready(id, file_id, now=None):
    return _set("projects", id, now, vod_file_id=file_id, vod_status="ready")


def record_download_click(id, now=None):
    now = time.time() if now is None else now
    _c().execute("UPDATE projects SET download_clicked_at=?, updated_at=? WHERE id=? AND download_clicked_at IS NULL",
                 (now, now, id))
    return get_project(id)


# -- tapes --

def create_tape(project_id, name, now=None):
    now = time.time() if now is None else now
    cur = _c().execute("INSERT INTO tapes(project_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                       (project_id, name, now, now))
    return get_tape(cur.lastrowid)


def get_tape(id):
    return _one("SELECT * FROM tapes WHERE id=?", (id,))


def list_tapes(project_id):
    return _all("SELECT * FROM tapes WHERE project_id=? ORDER BY id", (project_id,))


def update_tape(id, *, name=None, notes=None, status=None, now=None):
    if status is not None and status not in TAPE_STATUSES:
        raise ValueError(f"unknown tape status {status!r}")
    cols = {k: v for k, v in (("name", name), ("notes", notes), ("status", status)) if v is not None}
    return _set("tapes", id, now, **cols)


def set_tape_folder(id, folder_id):
    return _set("tapes", id, None, drive_folder_id=folder_id)


# -- iterations --

def next_version(tape_id):
    return _c().execute("SELECT COALESCE(MAX(version), 0) + 1 FROM iterations WHERE tape_id=?",
                        (tape_id,)).fetchone()[0]


def create_iteration(tape_id, filename, size, upload_url, now=None):
    now = time.time() if now is None else now
    cur = _c().execute(
        "INSERT INTO iterations(tape_id, version, filename, size, status, upload_url, created_at, updated_at)"
        " VALUES(?, (SELECT COALESCE(MAX(version), 0) + 1 FROM iterations WHERE tape_id=?),"
        " ?,?,'uploading',?,?,?)",
        (tape_id, tape_id, filename, size, upload_url, now, now))
    return get_iteration(cur.lastrowid)


def get_iteration(id):
    return _one("SELECT * FROM iterations WHERE id=?", (id,))


def list_iterations(tape_id):
    return _all("SELECT id, tape_id, version, filename, size, status, drive_file_id, upload_url,"
                " checks_json, uploaded_at, created_at, updated_at"
                " FROM iterations WHERE tape_id=? ORDER BY version", (tape_id,))


def complete_iteration(id, file_id, now=None, actor=None):
    """
    Returns (row, changed); changed is True only on the uploading/abandoned -> ready
    transition. An abandoned upload whose file turns up (and was verified by the
    caller) is recovered rather than refused. With an actor, the iteration_ready
    event is recorded in the same transaction, so it exists exactly when the status does.
    """
    now = time.time() if now is None else now
    _c()
    with db.transaction() as conn:
        row = get_iteration(id)
        if row is None:
            raise ValueError(f"no iteration {id}")
        if row["status"] == "ready":
            return row, False
        if row["status"] not in ("uploading", "abandoned"):
            raise ValueError(f"cannot complete an iteration that is {row['status']}")
        conn.execute("UPDATE iterations SET status='ready', drive_file_id=?, uploaded_at=?, updated_at=?"
                     " WHERE id=?", (file_id, now, now, id))
        conn.execute("UPDATE tapes SET status='in_review', updated_at=?"
                     " WHERE id=? AND status IN ('cutting','changes_asked')", (now, row["tape_id"]))
        if actor is not None:
            tape = get_tape(row["tape_id"])
            record_event(actor, "iteration_ready", f"{tape['name']} v{row['version']} is ready",
                         project_id=tape["project_id"], tape_id=tape["id"], iteration_id=id, now=now)
    return get_iteration(id), True


def abandon_iteration(id, now=None) -> bool:
    """uploading -> abandoned; False if it had already moved on (e.g. completed meanwhile)."""
    now = time.time() if now is None else now
    return _c().execute("UPDATE iterations SET status='abandoned', updated_at=? WHERE id=? AND status='uploading'",
                        (now, id)).rowcount == 1


def any_uploading() -> bool:
    return _c().execute("SELECT 1 FROM iterations WHERE status='uploading' LIMIT 1").fetchone() is not None


def set_iteration_media(id, *, checks_json=None, thumbnail=None):
    if thumbnail is not None and len(thumbnail) > THUMBNAIL_MAX_BYTES:
        raise ValueError("thumbnail too large")
    if checks_json is not None and len(checks_json.encode()) > CHECKS_MAX_BYTES:
        raise ValueError("checks too large")
    cols = {k: v for k, v in (("checks_json", checks_json), ("thumbnail", thumbnail)) if v is not None}
    return _set("iterations", id, None, **cols)


def previous_ready_iteration(tape_id, before_version):
    return _one("SELECT * FROM iterations WHERE tape_id=? AND status='ready' AND version<?"
                " ORDER BY version DESC LIMIT 1", (tape_id, before_version))


# -- events --

def record_event(actor, kind, summary, *, project_id=None, tape_id=None, iteration_id=None,
                 comment_id=None, now=None):
    now = time.time() if now is None else now
    return _c().execute(
        "INSERT INTO events(at, actor, kind, project_id, tape_id, iteration_id, comment_id, summary)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now, actor, kind, project_id, tape_id, iteration_id, comment_id, summary)).lastrowid


def events_since(after_id, exclude_actor=None):
    if exclude_actor is None:
        return _all("SELECT * FROM events WHERE id>? ORDER BY id", (after_id,))
    return _all("SELECT * FROM events WHERE id>? AND actor<>? ORDER BY id", (after_id, exclude_actor))
