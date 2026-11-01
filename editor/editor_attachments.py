"""Images on comments and replies: rows here, Drive flows in editor_service, bytes browser to Drive.

Each tape gets one "Attachments" folder inside its own Drive folder. The tape's file delete (and the
project delete) removes that folder with everything in it, so attachments never outlive their tape.
An attachment is uploaded first and linked to a comment or reply when that is posted.
"""
import time

from core import db
from editor import editor_store as es
from editor.editor_store import _all, _one

MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
MAX_BYTES = 20 * 1024 * 1024
MAX_PER_POST = 4
STALE_SECONDS = 86400  # an upload not completed in a day is dropped (its file, if any, goes with the tape)
REFUSED = "Images only (PNG, JPEG, WebP, GIF), up to 20 MB"

_DDL = """
CREATE TABLE IF NOT EXISTS attachments(
  id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL REFERENCES tapes(id),
  comment_id INTEGER, reply_id INTEGER, author TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
  size INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleted')),
  drive_file_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
"""
_FIELDS = "id, name, mime, size, drive_file_id"


def _c():
    es._c()
    db.ensure(_DDL)
    db.ensure(db._META_DDL)  # folder_of reads meta inside a delete's transaction, where ensure may not run
    return db.get()


def _folder_key(tape_id) -> str:
    return f"attachments_folder_{tape_id}"


def folder_of(tape_id) -> "str | None":
    """The tape's attachments folder; None when nothing was ever attached. Safe inside a transaction (after _c)."""
    row = db.get().execute("SELECT value FROM meta WHERE key=?", (_folder_key(tape_id),)).fetchone()
    return row["value"] if row else None


def set_folder(tape_id, folder_id) -> None:
    db.meta_set(_folder_key(tape_id), folder_id)


def validate(name, size, mime) -> str:
    if mime not in MIMES or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_BYTES:
        raise ValueError(REFUSED)
    if not isinstance(name, str) or not name.strip() or len(name) > 200 or "/" in name or "\\" in name:
        raise ValueError("The file name is not usable")
    return name.strip()


def create(tape_id, author, name, size, mime, now=None) -> int:
    now = time.time() if now is None else now
    conn = _c()
    with db.transaction():
        # A day-old upload nobody completed is dead; its file (if Google finished it) is deleted with the tape.
        conn.execute("UPDATE attachments SET status='deleted', updated_at=? WHERE status='uploading' AND created_at<?",
                     (now, now - STALE_SECONDS))
        return conn.execute(
            "INSERT INTO attachments(tape_id, author, name, mime, size, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,'uploading',?,?)", (tape_id, author, name, mime, size, now, now)).lastrowid


def forget(aid) -> None:
    """The Drive session could not be made: the row never existed as far as anyone knows."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM attachments WHERE id=? AND status='uploading'", (aid,))


def get(aid) -> "dict | None":
    _c()
    return _one("SELECT * FROM attachments WHERE id=?", (aid,))


def set_status(aid, status, file_id=None, now=None) -> None:
    now = time.time() if now is None else now
    with db.transaction() as conn:
        conn.execute("UPDATE attachments SET status=?, drive_file_id=COALESCE(?, drive_file_id), updated_at=?"
                     " WHERE id=? AND status='uploading'", (status, file_id, now, aid))


def link(conn, ids, author, tape_id, *, comment_id=None, reply_id=None) -> None:
    """Inside the post's transaction: every id must be the author's, ready, on this tape and still free."""
    if not ids:
        return
    if (not isinstance(ids, list) or len(ids) > MAX_PER_POST
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
        raise ValueError(f"attachment_ids must be a list of at most {MAX_PER_POST} ids")
    ids = sorted(set(ids))
    n = conn.execute(
        f"UPDATE attachments SET comment_id=?, reply_id=? WHERE id IN ({','.join('?' * len(ids))}) AND author=?"
        " AND tape_id=? AND status='ready' AND comment_id IS NULL AND reply_id IS NULL",
        (comment_id, reply_id, *ids, author, tape_id)).rowcount
    if n != len(ids):
        raise ValueError("An attachment is not ready, not yours, or already used")


def grouped(col, ids) -> dict:
    """{owner id: [attachment, ...]} for `col` comment_id or reply_id."""
    out: dict = {}
    if not ids:
        return out
    _c()
    rows = _all(f"SELECT {col} AS owner, {_FIELDS} FROM attachments WHERE {col} IN ({','.join('?' * len(ids))})"
                " AND status='ready' ORDER BY id", tuple(ids))
    for r in rows:
        out.setdefault(r.pop("owner"), []).append(r)
    return out


def mark_deleted(conn, tape_ids, now) -> None:
    """In a delete's finalize transaction (the caller ensured the schema): the folder is gone, so are its files."""
    if tape_ids:
        conn.execute(f"UPDATE attachments SET status='deleted', updated_at=? WHERE tape_id IN"
                     f" ({','.join('?' * len(tape_ids))})", (now, *tape_ids))
