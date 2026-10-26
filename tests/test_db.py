import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import db


def test_get_uses_wal_and_full_sync(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    conn = db.get()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    db.use_path_for_tests(tmp_path / "other.db")


def test_ensure_is_idempotent(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    ddl = "CREATE TABLE IF NOT EXISTS t(x)"
    db.ensure(ddl)
    db.ensure(ddl)
    assert db.get().execute("select name from sqlite_master where name='t'").fetchone()
    db.use_path_for_tests(tmp_path / "other.db")


def test_rows_are_addressable_by_name(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    assert db.get().execute("select 1 as one").fetchone()["one"] == 1
    db.use_path_for_tests(tmp_path / "other.db")


def test_get_turns_on_full_fsync_pragmas(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    conn = db.get()
    assert conn.execute("PRAGMA fullfsync").fetchone()[0] == 1
    assert conn.execute("PRAGMA checkpoint_fullfsync").fetchone()[0] == 1
    db.use_path_for_tests(tmp_path / "other.db")


def test_a_repeat_ensure_does_not_commit_an_open_transaction(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    ddl = "CREATE TABLE IF NOT EXISTS t(x)"
    db.ensure(ddl)
    conn = db.get()
    conn.execute("BEGIN")
    conn.execute("INSERT INTO t VALUES (1)")
    db.ensure(ddl)
    conn.execute("ROLLBACK")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    db.use_path_for_tests(tmp_path / "other.db")
