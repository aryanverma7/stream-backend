import sqlite3
import time

import pytest

import backup
import config
import db
import gdrive
import gdrive_oauth


def _ts(h, m, day=26):
    return time.mktime((2026, 10, day, h, m, 0, 0, 0, -1))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config.config, "_data", {})
    db.meta_set("gdrive_backups", "BK")


@pytest.fixture
def drive(monkeypatch):
    d = {"uploads": [], "deleted": [], "files": [], "fail": False}

    async def upload(path, name, parent, mime="x"):
        if d["fail"]:
            raise gdrive.DriveError("boom", 500)
        d["uploads"].append((name, parent, mime, sqlite3.connect(path).execute("PRAGMA integrity_check").fetchone()[0]))
        return "id"

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
    assert drive["deleted"] == ["i10", "i11", "i12"]


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
