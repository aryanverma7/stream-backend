import pytest

from core import config
from core import db
from editor import editor_service
from editor import editor_store as es
from editor import gdrive
from editor import reconcile
from core import scheduler

NOW = 10_000_000.0


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config.config, "_data", {})
    monkeypatch.setattr(scheduler, "clock_is_sane", lambda now: True)
    monkeypatch.setattr(reconcile, "_ran_since_boot", False)
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


@pytest.mark.asyncio
async def test_drive_error_on_one_iteration_does_not_stop_the_next(monkeypatch, tape):
    a = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW)
    b = es.create_iteration(tape["id"], "b.mp4", 5, "u", now=NOW)
    _stub(monkeypatch, [{"id": f"F{i['id']}", "appProperties": {"iteration_id": str(i["id"])}} for i in (a, b)])

    async def complete(iid, fid, actor):
        if iid == a["id"]:
            raise gdrive.DriveError("boom", 500)
        es.complete_iteration(iid, fid)

    monkeypatch.setattr(editor_service, "complete_iteration_upload", complete)
    assert await reconcile.run_once(NOW) == {"completed": 1, "abandoned": 0}
    assert es.get_iteration(b["id"])["status"] == "ready"


@pytest.mark.asyncio
async def test_not_connected_stops_the_pass_quietly(monkeypatch, tape, caplog):
    es.create_iteration(tape["id"], "a.mp4", 5, "u", now=0)

    async def list_folder(fid):
        raise gdrive.DriveNotConnected("no")

    monkeypatch.setattr(gdrive, "list_folder", list_folder)
    with caplog.at_level("INFO"):
        assert await reconcile.run_once(NOW) == {"completed": 0, "abandoned": 0}
    assert [r for r in caplog.records if r.name == "Reconcile" and r.levelno >= 30] == []


def test_abandon_is_guarded_on_uploading(tape):
    it = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=0)
    es.complete_iteration(it["id"], "F")
    assert es.abandon_iteration(it["id"], now=NOW) is False
    assert es.get_iteration(it["id"])["status"] == "ready"


@pytest.mark.asyncio
async def test_job_runs_on_boot_then_only_when_uploading_and_every_600s(monkeypatch, tape):
    runs = []

    async def run_once(now):
        runs.append(now)
        return {}

    monkeypatch.setattr(reconcile, "run_once", run_once)
    monkeypatch.setattr(reconcile, "_ran_since_boot", False)
    await reconcile.run_if_due(NOW)  # first sane tick: always, even with nothing uploading
    await reconcile.run_if_due(NOW + 900)  # nothing uploading
    assert runs == [NOW]
    es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW)
    await reconcile.run_if_due(NOW + 1000)
    await reconcile.run_if_due(NOW + 1599)
    await reconcile.run_if_due(NOW + 1600)
    assert runs == [NOW, NOW + 1000, NOW + 1600]


@pytest.mark.asyncio
async def test_reconcile_measures_abandon_from_the_last_renewal(monkeypatch, tape):
    renewed = es.create_iteration(tape["id"], "a.mp4", 5, "u", now=NOW - reconcile.ABANDON_AFTER_SECONDS - 1)
    db.get().execute("UPDATE iterations SET updated_at=? WHERE id=?", (NOW - 10, renewed["id"]))
    _stub(monkeypatch, [])
    assert (await reconcile.run_once(NOW))["abandoned"] == 0
    assert es.get_iteration(renewed["id"])["status"] == "uploading"
