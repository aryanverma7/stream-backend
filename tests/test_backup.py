import sqlite3
import time

import pytest

from editor import backup
from core import config
from core import db
from editor import gdrive
from editor import gdrive_oauth


def _ts(h, m, day=26):
    return time.mktime((2026, 10, day, h, m, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config.config, "_data", {"gdrive_refresh_token": "rt"})
    monkeypatch.setattr(backup, "_not_connected_logged", False, raising=False)
    db.meta_set("gdrive_backups", "BK")


@pytest.fixture
def drive(monkeypatch):
    d = {"uploads": [], "deleted": [], "files": [], "fail": False, "calls": 0}

    async def upload(path, name, parent, mime="x"):
        d["calls"] += 1
        if d["fail"]:
            raise d["fail"] if isinstance(d["fail"], Exception) else gdrive.DriveError("boom", 500)
        d["uploads"].append((name, parent, mime, sqlite3.connect(path).execute("PRAGMA integrity_check").fetchone()[0]))
        d["files"].append({"id": "new", "name": name})
        return "new"

    async def list_folder(fid):
        return d["files"]

    async def delete(fid):
        d["deleted"].append(fid)

    monkeypatch.setattr(gdrive, "upload_small_file", upload)
    monkeypatch.setattr(gdrive, "list_folder", list_folder)
    monkeypatch.setattr(gdrive, "delete_file", delete)
    return d


@pytest.mark.asyncio
async def test_backup_runs_once_per_day_after_4am(drive):
    await backup.run_if_due(_ts(3, 59))
    assert drive["uploads"] == []
    await backup.run_if_due(_ts(4, 1))
    assert drive["uploads"] == [("app-2026-10-26.db", "BK", "application/x-sqlite3", "ok")]
    await backup.run_if_due(_ts(5, 0))
    assert len(drive["uploads"]) == 1
    await backup.run_if_due(_ts(4, 1, day=27))
    assert len(drive["uploads"]) == 2


@pytest.mark.asyncio
async def test_backup_failure_retries_next_tick(drive):
    drive["fail"] = True
    await backup.run_if_due(_ts(4, 1))
    assert db.meta_get("backup_last_date") is None
    drive["fail"] = False
    await backup.run_if_due(_ts(4, 2))
    assert db.meta_get("backup_last_date") == "2026-10-26"


@pytest.mark.asyncio
async def test_backup_keeps_newest_seven(drive):
    drive["files"] = [{"id": f"i{d}", "name": f"app-2026-10-{d:02d}.db"} for d in range(10, 20)]
    drive["files"].append({"id": "other", "name": "notes.txt"})
    await backup.run_if_due(_ts(4, 1))
    # 10 old + today's upload: the four oldest go, leaving exactly seven.
    assert drive["deleted"] == ["i10", "i11", "i12", "i13"]


@pytest.mark.asyncio
async def test_backup_creates_layout_when_missing(drive, monkeypatch):
    get = db.get()
    get.execute("DELETE FROM meta WHERE key='gdrive_backups'")

    async def layout():
        db.meta_set("gdrive_backups", "NEW")

    monkeypatch.setattr(gdrive_oauth, "ensure_layout", layout)
    await backup.run_if_due(_ts(4, 1))
    assert drive["uploads"][0][1] == "NEW"


@pytest.mark.asyncio
async def test_backup_prune_failure_keeps_date(drive, monkeypatch):
    async def bad(fid):
        raise gdrive.DriveError("x", 500)

    monkeypatch.setattr(gdrive, "list_folder", bad)
    await backup.run_if_due(_ts(4, 1))
    assert db.meta_get("backup_last_date") == "2026-10-26"


@pytest.fixture
def copies(monkeypatch):
    made = []
    real = backup.tempfile.TemporaryDirectory

    def spy(*a, **k):
        made.append(1)
        return real(*a, **k)

    monkeypatch.setattr(backup.tempfile, "TemporaryDirectory", spy)
    return made


@pytest.mark.asyncio
async def test_backup_without_drive_token_does_nothing(drive, copies, monkeypatch, caplog):
    monkeypatch.setattr(config.config, "_data", {})
    called = []

    async def layout():
        called.append(1)

    monkeypatch.setattr(gdrive_oauth, "ensure_layout", layout)
    db.get().execute("DELETE FROM meta WHERE key='gdrive_backups'")
    with caplog.at_level("INFO"):
        await backup.run_if_due(_ts(4, 1))
    assert (copies, called, drive["calls"]) == ([], [], 0)
    assert [r for r in caplog.records if r.name == "Backup"] == []


@pytest.mark.asyncio
async def test_backup_folder_resolved_before_copy(drive, copies, monkeypatch):
    db.get().execute("DELETE FROM meta WHERE key='gdrive_backups'")

    async def layout():
        raise gdrive.DriveError("down", 500)

    monkeypatch.setattr(gdrive_oauth, "ensure_layout", layout)
    await backup.run_if_due(_ts(4, 1))
    assert copies == [] and drive["calls"] == 0


@pytest.mark.asyncio
async def test_backup_failure_backs_off(drive, caplog):
    drive["fail"] = True
    t = _ts(4, 1)
    with caplog.at_level("INFO"):
        await backup.run_if_due(t)
        assert drive["calls"] == 1 and float(db.meta_get("backup_next_try")) == t + 30
        await backup.run_if_due(t + 29)  # inside the backoff: nothing at all
        assert drive["calls"] == 1
        await backup.run_if_due(t + 30)
        assert drive["calls"] == 2 and float(db.meta_get("backup_next_try")) == t + 30 + 60
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 2
    drive["fail"] = False
    await backup.run_if_due(t + 90)
    assert db.meta_get("backup_last_date") == "2026-10-26" and db.meta_get("backup_attempts") == "0"


@pytest.mark.asyncio
async def test_drive_not_connected_logs_info_once(drive, caplog):
    drive["fail"] = gdrive.DriveNotConnected("refused")
    t = _ts(4, 1)
    with caplog.at_level("INFO"):
        await backup.run_if_due(t)
        await backup.run_if_due(t + 3600)
    assert drive["calls"] == 2
    recs = [r for r in caplog.records if r.name == "Backup"]
    assert [r.levelname for r in recs] == ["INFO"]
