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
(isolation_level=None); use `with conn:` or an explicit BEGIN for multi-statement
changes. One shared connection: never hold a transaction open across an await.
The connection belongs to the event-loop thread; executor code must not use it
(pass results back to the loop instead) - sqlite3 raises if another thread tries.
"""
import sqlite3
from pathlib import Path

from config import config

DEFAULT_PATH = Path(__file__).parent / "app.db"

_conn: sqlite3.Connection | None = None
_path_override: Path | None = None
_ensured: set[str] = set()  # executescript COMMITs, so each DDL runs once per connection


def get() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = _path_override or config.get("app_db_path") or DEFAULT_PATH
        conn = sqlite3.connect(path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA fullfsync=ON")
        conn.execute("PRAGMA checkpoint_fullfsync=ON")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn = conn
    return _conn


def ensure(ddl: str) -> None:
    """Run idempotent CREATE TABLE IF NOT EXISTS statements; each module calls this once for its own tables."""
    if ddl not in _ensured:
        get().executescript(ddl)
        _ensured.add(ddl)


def use_path_for_tests(path: Path) -> None:
    global _conn, _path_override
    _ensured.clear()
    if _conn is not None:
        _conn.close()
        _conn = None
    _path_override = path
