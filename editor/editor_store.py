"""Editor app store: projects (one per stream VOD, or one per short upload, a tape), edits, iterations and the events log.

An edit is any output: a video or a clip (made from a VOD) or a reel (made from a tape). The UI never says "edit".
A tape (an upload shorter than TAPE_MAX_SECONDS) gets exactly one reel: an edit of kind 'reel'.
reel_sources maps tapes to the reel they are in; its UNIQUE project_id keeps a tape in one reel.
"""
import time

from core import db

THUMBNAIL_MAX_BYTES = 262144
CHECKS_MAX_BYTES = 16384
EDIT_STATUSES = ("cutting", "in_review", "changes_asked", "final")
TAPE_MAX_SECONDS = 300  # a shorter upload is a tape (gets one reel), a longer one a VOD
SHORT_MAX_SECONDS = 180  # clips and reels: vertical and under this

_DDL = """
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, stream_date TEXT NOT NULL,
  stream_title TEXT NOT NULL DEFAULT '', vod_filename TEXT NOT NULL, vod_size INTEGER,
  vod_file_id TEXT,
  vod_status TEXT NOT NULL CHECK (vod_status IN ('uploading','ready','deleting','deleted')),
  vod_upload_url TEXT, drive_folder_id TEXT, download_clicked_at REAL, deleted_at REAL,
  kind TEXT NOT NULL DEFAULT 'vod' CHECK (kind IN ('vod','tape')), duration_s REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS edits(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL, kind TEXT CHECK (kind IN ('video','clip','reel')),
  status TEXT NOT NULL DEFAULT 'cutting' CHECK (status IN ('cutting','in_review','changes_asked','final')),
  notes TEXT NOT NULL DEFAULT '', drive_folder_id TEXT, files_deleted_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS iterations(
  id INTEGER PRIMARY KEY, edit_id INTEGER NOT NULL REFERENCES edits(id),
  version INTEGER NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleting','deleted','abandoned')),
  drive_file_id TEXT, upload_url TEXT, checks_json TEXT, thumbnail BLOB, uploaded_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (edit_id, version));
CREATE TABLE IF NOT EXISTS reel_sources(
  edit_id INTEGER NOT NULL REFERENCES edits(id),
  project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id), PRIMARY KEY (edit_id, project_id));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  project_id INTEGER, edit_id INTEGER, iteration_id INTEGER, comment_id INTEGER,
  summary TEXT NOT NULL);
"""


# Quota columns (editor_quota.py): when an edit became Final, the upload time of the version that was Final
# (the week that gets the credit), and the streamer's "doesn't count" switch.
_EDIT_ALTERS = (("final_at", "REAL"), ("final_upload_at", "REAL"), ("quota_exempt", "INTEGER NOT NULL DEFAULT 0"))


def _rebuild(conn, table, kinds, extra=()):
    """Recreate `table` with its `kind` CHECK values renamed (SQLite cannot alter a CHECK), keeping every row."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    for old, new in (*((f"'{o}'", f"'{n}'") for o, n in kinds.items()), *extra):
        sql = sql.replace(old, new)
    conn.execute(f"CREATE TABLE {table}_new" + sql[sql.index("("):])
    cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
    case = "CASE kind " + " ".join(f"WHEN '{o}' THEN '{n}'" for o, n in kinds.items()) + " ELSE kind END"
    conn.execute(f"INSERT INTO {table}_new SELECT {', '.join(case if c == 'kind' else c for c in cols)} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_new RENAME TO {table}")


def _migrate():
    """An app.db from before the 2026-11-25 words: tapes -> edits (kind tape -> video), project kind
    clip -> tape, every tape_id column -> edit_id, tape_seen -> edit_seen, the tape_files delete kind and
    the tape_created event kind. One transaction, so a power cut leaves either schema whole; no-op once done."""
    conn = db.get()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "tapes" not in tables:
        return
    conn.execute("PRAGMA foreign_keys=OFF")  # a no-op inside a transaction; DROP TABLE would cascade otherwise
    try:
        with db.transaction():
            conn.execute("ALTER TABLE tapes RENAME TO edits")  # also repoints every REFERENCES tapes(id)
            _rebuild(conn, "edits", {"tape": "video"}, (("kind TEXT NOT NULL DEFAULT 'video' CHECK (kind IN ('video','reel'))",
                                                          "kind TEXT CHECK (kind IN ('video','clip','reel'))"),))
            # no version yet: no kind, the first upload decides it
            conn.execute("UPDATE edits SET kind=NULL WHERE kind='video'"
                         " AND NOT EXISTS (SELECT 1 FROM iterations i WHERE i.tape_id=edits.id)")
            _rebuild(conn, "projects", {"clip": "tape"})
            if "delete_requests" in tables:
                _rebuild(conn, "delete_requests", {"tape_files": "edit_files"})
            if "tape_seen" in tables:
                conn.execute("ALTER TABLE tape_seen RENAME TO edit_seen")
            for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if "tape_id" in {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}:
                    conn.execute(f"ALTER TABLE {t} RENAME COLUMN tape_id TO edit_id")
            conn.execute("UPDATE events SET kind='edit_created' WHERE kind='tape_created'")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("editor schema migration broke a foreign key; nothing was changed")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _c():
    if _DDL in db._ensured:  # the column check below runs once per database, not on every query
        return db.get()
    _migrate()  # before the DDL, which would otherwise create an empty edits table beside tapes
    db.ensure(_DDL)
    conn = db.get()
    have = {r["name"] for r in conn.execute("PRAGMA table_info(edits)")}
    for col, decl in _EDIT_ALTERS:
        if col not in have:
            db.ensure(f"ALTER TABLE edits ADD COLUMN {col} {decl};")
            if col == "final_upload_at":  # edits already Final: credit the week their last version was uploaded
                db.ensure("UPDATE edits SET final_upload_at=(SELECT MAX(COALESCE(i.uploaded_at, i.created_at))"
                          " FROM iterations i WHERE i.edit_id=edits.id AND i.status IN ('ready','deleted'))"
                          " WHERE status='final';")
    return conn


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

def create_project(name, stream_date, stream_title, vod_filename, vod_size, now=None, duration_s=None,
                   meta_key=None):
    """duration_s under TAPE_MAX_SECONDS makes a tape, which gets its one reel in the same transaction.
    meta_key, when given, is set to the new project's id in that transaction too (the uploader's job id)."""
    now = time.time() if now is None else now
    kind = "tape" if duration_s is not None and duration_s < TAPE_MAX_SECONDS else "vod"
    _c()
    if meta_key:
        db.ensure(db._META_DDL)  # DDL before the transaction
    with db.transaction() as conn:
        pid = conn.execute(
            "INSERT INTO projects(name, stream_date, stream_title, vod_filename, vod_size, vod_status, kind,"
            " duration_s, created_at, updated_at) VALUES(?,?,?,?,?,'uploading',?,?,?,?)",
            (name, stream_date, stream_title, vod_filename, vod_size, kind, duration_s, now, now)).lastrowid
        if kind == "tape":
            insert_reel(conn, pid, name, now)
        if meta_key:
            db.meta_set(meta_key, str(pid))
    return get_project(pid)


def insert_reel(conn, project_id, name, now):
    """A tape's own reel and its reel_sources row. Call inside a transaction."""
    tid = conn.execute("INSERT INTO edits(project_id, name, kind, created_at, updated_at) VALUES(?,?,'reel',?,?)",
                       (project_id, name, now, now)).lastrowid
    conn.execute("INSERT INTO reel_sources(edit_id, project_id) VALUES(?,?)", (tid, project_id))
    return tid


_PROJECT = "SELECT p.*, (SELECT edit_id FROM reel_sources r WHERE r.project_id=p.id) AS reel_edit_id FROM projects p"


def get_project(id):
    return _one(f"{_PROJECT} WHERE p.id=?", (id,))


def list_projects():
    return _all(f"SELECT x.*, (SELECT COUNT(*) FROM edits t WHERE t.project_id=x.id) AS edit_count FROM ({_PROJECT}) x"
                " WHERE deleted_at IS NULL ORDER BY created_at DESC, id DESC")


def rename_project(id, name, now=None):
    _set("projects", id, now, name=name)
    return get_project(id)


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


# -- edits --

def create_edit(project_id, name, now=None):
    """An output made from a VOD. It has no kind until its first version: detect_kind decides video or clip."""
    now = time.time() if now is None else now
    p = get_project(project_id)
    if p is not None and p["kind"] == "tape":
        raise ValueError("A tape has exactly one reel")
    cur = _c().execute("INSERT INTO edits(project_id, name, created_at, updated_at) VALUES(?,?,?,?)",
                       (project_id, name, now, now))
    return get_edit(cur.lastrowid)


_SHAPE = {"clip": f"vertical (taller than wide) and under {SHORT_MAX_SECONDS // 60} minutes",
          "reel": f"vertical (taller than wide) and under {SHORT_MAX_SECONDS // 60} minutes",
          "video": f"horizontal (wider than tall) and {SHORT_MAX_SECONDS // 60} minutes or longer"}


def detect_kind(edit, width, height, duration_s, now=None, save=True) -> str:
    """The kind a new version of `edit` makes, from the browser's probe of the file; ValueError if it does not fit.

    A reel must be vertical and under SHORT_MAX_SECONDS. A VOD's output takes its kind from its first version
    (under SHORT_MAX_SECONDS a clip, which must be vertical; otherwise a video, which must be horizontal) and
    every later version must keep it. Only abandoned uploads so far: the kind is detected afresh."""
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in (width, height, duration_s)):
        raise ValueError("width, height and duration_s of the file are needed")
    shaped = "clip" if duration_s < SHORT_MAX_SECONDS and height > width else \
        "video" if duration_s >= SHORT_MAX_SECONDS and width > height else None
    kept = edit["kind"]
    if kept != "reel" and not _c().execute(
            "SELECT 1 FROM iterations WHERE edit_id=? AND status!='abandoned' LIMIT 1", (edit["id"],)).fetchone():
        kept = None
    if kept == "reel":
        if shaped != "clip":
            raise ValueError(f"A reel must be {_SHAPE['reel']} - this file is not")
        return "reel"
    if shaped is None:
        raise ValueError(f"This file is neither a clip ({_SHAPE['clip']}) nor a video ({_SHAPE['video']})")
    if kept is not None and shaped != kept:
        raise ValueError(f"This is a {kept}: every version must be {_SHAPE[kept]} - this file is not")
    if save and edit["kind"] != shaped:
        _set("edits", edit["id"], now, kind=shaped)
    return shaped


def _with_sources(edit):
    if edit is not None:
        edit["source_project_ids"] = [r[0] for r in _c().execute(
            "SELECT r.project_id FROM reel_sources r JOIN projects p ON p.id=r.project_id WHERE r.edit_id=?"
            " ORDER BY p.created_at, p.id", (edit["id"],))]
    return edit


def get_edit(id):
    return _with_sources(_one("SELECT * FROM edits WHERE id=?", (id,)))


def list_edits(project_id):
    return [_with_sources(t) for t in _all("SELECT * FROM edits WHERE project_id=? ORDER BY id", (project_id,))]


def reel_for_tape(project_id):
    return _with_sources(_one("SELECT t.* FROM edits t JOIN reel_sources r ON r.edit_id=t.id WHERE r.project_id=?",
                              (project_id,)))


def update_edit(id, *, name=None, notes=None, status=None, quota_exempt=None, now=None):
    if status is not None and status not in EDIT_STATUSES:
        raise ValueError(f"unknown edit status {status!r}")
    now = time.time() if now is None else now
    cols = {k: v for k, v in (("name", name), ("notes", notes), ("status", status)) if v is not None}
    if quota_exempt is not None:
        cols["quota_exempt"] = 1 if quota_exempt else 0
    was = get_edit(id) if status is not None else None
    if was and status == "final" and was["status"] != "final":
        # The quota credits the week the Final version was uploaded, so a late approval never costs the editor.
        up = _one("SELECT MAX(COALESCE(uploaded_at, created_at)) AS at FROM iterations WHERE edit_id=? AND status='ready'",
                  (id,))
        cols["final_at"] = now
        cols["final_upload_at"] = up["at"] if up and up["at"] is not None else now
    elif was and status != "final" and was["status"] == "final":
        cols["final_at"] = None
        cols["final_upload_at"] = None
    _set("edits", id, now, **cols)
    return get_edit(id)


def set_edit_folder(id, folder_id):
    return _set("edits", id, None, drive_folder_id=folder_id)


# -- iterations --

def next_version(edit_id):
    return _c().execute("SELECT COALESCE(MAX(version), 0) + 1 FROM iterations WHERE edit_id=?",
                        (edit_id,)).fetchone()[0]


def create_iteration(edit_id, filename, size, upload_url, now=None):
    now = time.time() if now is None else now
    cur = _c().execute(
        "INSERT INTO iterations(edit_id, version, filename, size, status, upload_url, created_at, updated_at)"
        " VALUES(?, (SELECT COALESCE(MAX(version), 0) + 1 FROM iterations WHERE edit_id=?),"
        " ?,?,'uploading',?,?,?)",
        (edit_id, edit_id, filename, size, upload_url, now, now))
    return get_iteration(cur.lastrowid)


def get_iteration(id):
    return _one("SELECT * FROM iterations WHERE id=?", (id,))


def list_iterations(edit_id):
    return _all("SELECT id, edit_id, version, filename, size, status, drive_file_id, upload_url,"
                " checks_json, uploaded_at, created_at, updated_at"
                " FROM iterations WHERE edit_id=? ORDER BY version", (edit_id,))


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
        conn.execute("UPDATE edits SET status='in_review', updated_at=?"
                     " WHERE id=? AND status IN ('cutting','changes_asked')", (now, row["edit_id"]))
        if actor is not None:
            edit = get_edit(row["edit_id"])
            record_event(actor, "iteration_ready", f"{edit['name']} v{row['version']} is ready",
                         project_id=edit["project_id"], edit_id=edit["id"], iteration_id=id, now=now)
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


def previous_ready_iteration(edit_id, before_version):
    return _one("SELECT * FROM iterations WHERE edit_id=? AND status='ready' AND version<?"
                " ORDER BY version DESC LIMIT 1", (edit_id, before_version))


# -- events --

def record_event(actor, kind, summary, *, project_id=None, edit_id=None, iteration_id=None,
                 comment_id=None, now=None):
    now = time.time() if now is None else now
    return _c().execute(
        "INSERT INTO events(at, actor, kind, project_id, edit_id, iteration_id, comment_id, summary)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (now, actor, kind, project_id, edit_id, iteration_id, comment_id, summary)).lastrowid


def events_since(after_id, exclude_actor=None):
    if exclude_actor is None:
        return _all("SELECT * FROM events WHERE id>? ORDER BY id", (after_id,))
    return _all("SELECT * FROM events WHERE id>? AND actor<>? ORDER BY id", (after_id, exclude_actor))
