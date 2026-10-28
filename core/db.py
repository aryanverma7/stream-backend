"""
App database: one SQLite file shared by login sessions and the editor app.

WAL + synchronous=FULL + fullfsync because this runs on a 2012 Mac Mini that
loses power without warning. On macOS a plain fsync only reaches the drive's
cache; PRAGMA fullfsync / checkpoint_fullfsync make commits and checkpoints use
F_FULLFSYNC, so a commit that returned really is on disk (elsewhere the pragmas
are harmless no-ops). WAL means a cut mid-write rolls back to the last commit
instead of corrupting the file. (The default synchronous=NORMAL under WAL can
drop the last commits on power loss, which for a login session means a
surprise logout.)

One process-wide connection, opened lazily. It is autocommit
(isolation_level=None); use `with db.transaction():` for multi-statement changes;
`with conn:` does nothing on this connection. One shared connection: never hold a transaction open across an await.
The connection belongs to the event-loop thread; executor code must not use it
(pass results back to the loop instead) - sqlite3 raises if another thread tries.
"""
import contextlib
import sqlite3
from pathlib import Path

from core.config import config
from core.logger import get_logger

DEFAULT_PATH = Path(__file__).parent.parent / "app.db"

_conn: sqlite3.Connection | None = None
_path_override: Path | None = None
_ensured: set[str] = set()  # executescript COMMITs, so each DDL runs once per connection


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")  # first real I/O: fails here for a directory or unwritable file
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        conn.close()
        raise
    return conn


def get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        if _path_override:
            _conn = _open(_path_override)  # tests: never fall back onto the real app.db
            return _conn
        configured = config.get("app_db_path")
        if configured:
            path = configured
            try:
                path = Path(configured)
                if not path.is_absolute():
                    path = Path(__file__).parent.parent / path
                _conn = _open(path)
            except (TypeError, ValueError):
                get_logger("db").warning("app_db_path %r is not a usable path; using %s", path, DEFAULT_PATH)
            except sqlite3.OperationalError as e:
                # Only "cannot open" falls back; corruption or a lock must not silently swap in a fresh DB.
                msg = str(e).lower()
                if "unable to open" not in msg and "readonly" not in msg:
                    get_logger("db").error("app_db_path %s failed: %s", path, e)
                    raise
                get_logger("db").warning("app_db_path %s cannot be opened; using %s", path, DEFAULT_PATH)
            except sqlite3.Error as e:
                get_logger("db").error("app_db_path %s failed: %s", path, e)
                raise
        if _conn is None:
            _conn = _open(DEFAULT_PATH)
    return _conn


@contextlib.contextmanager
def transaction():
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK and re-raise. No awaits inside."""
    conn = get()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


_META_DDL = "CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"


def meta_get(key: str, default: str | None = None) -> str | None:
    ensure(_META_DDL)
    row = get().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def meta_set(key: str, value: str) -> None:
    ensure(_META_DDL)
    get().execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def meta_delete(key: str) -> None:
    ensure(_META_DDL)
    get().execute("DELETE FROM meta WHERE key=?", (key,))


def ensure(ddl: str) -> None:
    """Run idempotent CREATE TABLE IF NOT EXISTS statements; each module calls this once for its own tables."""
    if ddl not in _ensured:
        conn = get()
        if conn.in_transaction:  # executescript would COMMIT the caller's transaction
            raise RuntimeError("db.ensure called inside a transaction")
        conn.executescript(ddl)
        _ensured.add(ddl)


def use_path_for_tests(path: Path) -> None:
    global _conn, _path_override
    _ensured.clear()
    if _conn is not None:
        _conn.close()
        _conn = None
    _path_override = path
