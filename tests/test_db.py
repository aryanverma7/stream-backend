import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

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


def test_bad_db_path_falls_back_to_default(tmp_path, monkeypatch):
    from config import config
    db.use_path_for_tests(tmp_path / "x.db")
    db._path_override = None
    monkeypatch.setitem(config._data, "app_db_path", str(tmp_path / "missing" / "x.db"))
    monkeypatch.setattr(db, "DEFAULT_PATH", tmp_path / "fallback.db")
    assert db.get().execute("select 1").fetchone()[0] == 1
    assert (tmp_path / "fallback.db").exists()
    db.use_path_for_tests(tmp_path / "other.db")


def test_relative_db_path_resolves_next_to_the_module(tmp_path, monkeypatch):
    from config import config
    db.use_path_for_tests(tmp_path / "x.db")
    db._path_override = None
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(config._data, "app_db_path", "rel_test.db")
    target = Path(db.__file__).parent / "rel_test.db"
    try:
        db.get()
        assert target.exists()
        assert not (tmp_path / "rel_test.db").exists()
    finally:
        db.use_path_for_tests(tmp_path / "other.db")
        for suffix in ("", "-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)


def test_meta_roundtrip_and_upsert(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    db.meta_set("a", "1")
    db.meta_set("a", "2")
    assert db.meta_get("a") == "2"
    assert db.meta_get("b", "x") == "x"
    assert db.meta_get("b") is None
    db.use_path_for_tests(tmp_path / "other.db")


def _configured(tmp_path, monkeypatch, value):
    from config import config
    db.use_path_for_tests(tmp_path / "x.db")
    db._path_override = None
    monkeypatch.setitem(config._data, "app_db_path", value)
    monkeypatch.setattr(db, "DEFAULT_PATH", tmp_path / "fallback.db")


def test_corrupt_db_at_configured_path_raises_without_fallback(tmp_path, monkeypatch):
    import pytest
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a db" * 100)
    _configured(tmp_path, monkeypatch, str(bad))
    with pytest.raises(Exception):
        db.get()
    assert not (tmp_path / "fallback.db").exists()
    db.use_path_for_tests(tmp_path / "other.db")


def test_test_override_that_cannot_open_raises(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(db, "DEFAULT_PATH", tmp_path / "fallback.db")
    db.use_path_for_tests(tmp_path / "missing" / "x.db")
    with pytest.raises(Exception):
        db.get()
    assert not (tmp_path / "fallback.db").exists()
    db.use_path_for_tests(tmp_path / "other.db")


def test_non_string_db_path_falls_back(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch, 123)
    db.get()
    assert (tmp_path / "fallback.db").exists()
    db.use_path_for_tests(tmp_path / "other.db")


def test_nul_in_db_path_falls_back(tmp_path, monkeypatch):
    _configured(tmp_path, monkeypatch, "a\0b.db")
    db.get()
    assert (tmp_path / "fallback.db").exists()
    db.use_path_for_tests(tmp_path / "other.db")


def test_transaction_rolls_back_on_error(tmp_path):
    import pytest
    db.use_path_for_tests(tmp_path / "t.db")
    conn = db.get()
    conn.execute("CREATE TABLE tx(x)")
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            assert c.in_transaction
            c.execute("INSERT INTO tx VALUES(1)")
            raise RuntimeError
    assert not conn.in_transaction
    assert conn.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == 0
    with db.transaction() as c:
        c.execute("INSERT INTO tx VALUES(2)")
    assert conn.execute("SELECT COUNT(*) FROM tx").fetchone()[0] == 1
    db.use_path_for_tests(tmp_path / "other.db")


def test_meta_delete(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    db.meta_set("a", "1")
    db.meta_delete("a")
    db.meta_delete("never-existed")
    assert db.meta_get("a") is None
    db.use_path_for_tests(tmp_path / "other.db")


def test_ensure_inside_transaction_raises(tmp_path):
    db.use_path_for_tests(tmp_path / "t.db")
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.ensure("CREATE TABLE IF NOT EXISTS late(x)")
    db.use_path_for_tests(tmp_path / "other.db")
