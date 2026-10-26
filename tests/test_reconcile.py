import pytest

import config
import db
import editor_service
import editor_store as es
import gdrive
import reconcile
import scheduler

NOW = 10_000_000.0


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config.config, "_data", {})
    monkeypatch.setattr(scheduler, "clock_is_sane", lambda now: True)
    p = es.create_project("P", "2026-10-01", "t", "v.mp4", 1, now=0)
    t = es.create_tape(p["id"], "T", now=0)
    es.set_tape_folder(t["id"], "TF")
    return t


@pytest.fixture
def tape():
    return es.list_tapes(1)[0]


def _stub(monkeypatch, files, completed=None, fail=False):
    deleted = []

    async def list_folder(fid):
        if fail:
            raise gdrive.DriveError("boom", 500)
        return files

    async def complete(iid, fid, actor):
        if completed is not None:
            completed.append((iid, fid, actor))
        es.complete_iteration(iid, fid)

    async def delete(fid):
        deleted.append(fid)

    monkeypatch.setattr(gdrive, "list_folder", list_folder)
    monkeypatch.setattr(gdrive, "delete_file", delete)
    monkeypatch.setattr(editor_service, "complete_iteration_upload", complete)
    return deleted


@pytest.mark.asyncio
async def test_reconcile_completes_upload_found_in_folder(monkeypatch, tape):
    it = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW)
    done = []
    _stub(monkeypatch, [{"id": "F1", "appProperties": {"iteration_id": str(it["id"])}}], done)
    assert await reconcile.run_once(NOW) == {"completed": 1, "abandoned": 0}
    assert done == [(it["id"], "F1", "system")]
    assert es.get_iteration(it["id"])["status"] == "ready"


@pytest.mark.asyncio
async def test_reconcile_abandons_stale_upload(monkeypatch, tape):
    old = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW - reconcile.ABANDON_AFTER_SECONDS - 1)
    fresh = es.create_iteration(tape["id"], "b.mp4", 5, "u", now=NOW - 10)
    _stub(monkeypatch, [])
    assert await reconcile.run_once(NOW) == {"completed": 0, "abandoned": 1}
    assert es.get_iteration(old["id"])["status"] == "abandoned"
    assert es.get_iteration(fresh["id"])["status"] == "uploading"


@pytest.mark.asyncio
async def test_reconcile_does_not_abandon_on_insane_clock(monkeypatch, tape):
    old = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=0)
    _stub(monkeypatch, [])
    monkeypatch.setattr(scheduler, "clock_is_sane", lambda now: False)
    assert (await reconcile.run_once(NOW))["abandoned"] == 0
    assert es.get_iteration(old["id"])["status"] == "uploading"


@pytest.mark.asyncio
async def test_reconcile_never_deletes_unknown_files(monkeypatch, tape):
    es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW)
    deleted = _stub(monkeypatch, [{"id": "X", "name": "stray", "appProperties": {}}])
    await reconcile.run_once(NOW)
    assert deleted == []


@pytest.mark.asyncio
async def test_reconcile_survives_drive_error(monkeypatch, tape):
    old = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=0)
    _stub(monkeypatch, [], fail=True)
    assert await reconcile.run_once(NOW) == {"completed": 0, "abandoned": 0}
    assert es.get_iteration(old["id"])["status"] == "uploading"


@pytest.mark.asyncio
async def test_reconcile_skips_mismatched_file(monkeypatch, tape):
    it = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=0)
    _stub(monkeypatch, [{"id": "F1", "appProperties": {"iteration_id": str(it["id"])}}])

    async def bad(iid, fid, actor):
        raise ValueError("size")

    monkeypatch.setattr(editor_service, "complete_iteration_upload", bad)
    assert await reconcile.run_once(NOW) == {"completed": 0, "abandoned": 0}
