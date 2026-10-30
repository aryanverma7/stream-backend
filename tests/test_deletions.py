import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import db
from editor import deletions
from editor import editor_store as es
from editor import gdrive

T0 = 1_000_000.0


@pytest.fixture(autouse=True)
def _fresh(tmp_path):
    db.use_path_for_tests(tmp_path / "d.db")
    deletions._reset_for_tests()
    deletions._grace_applied = True  # most tests are mid-process; the grace test resets this
    yield
    deletions._reset_for_tests()


@pytest.fixture
def drive(monkeypatch):
    """Records delete_file calls; set drive.fail to an exception to make every call raise it."""
    class D:
        calls: list = []
        fail = None

    async def fake_delete(file_id):
        D.calls.append(file_id)
        if D.fail is not None:
            raise D.fail

    D.calls = []
    monkeypatch.setattr(gdrive, "delete_file", fake_delete)
    return D


def _project(downloaded=True):
    p = es.create_project("Ranked grind", "2026-10-20", "t", "vod-1020.mp4", 100, now=T0)
    es.mark_vod_ready(p["id"], "VODFILE", now=T0)
    es.set_project_folder(p["id"], "PFOLDER")
    if downloaded:
        es.record_download_click(p["id"], now=T0)
    return es.get_project(p["id"])


def _tape_with_iterations(project_id, uploading=False):
    t = es.create_tape(project_id, "Ace clip", now=T0)
    i1 = es.create_iteration(t["id"], "a.mp4", 1, "u", now=T0)
    es.complete_iteration(i1["id"], "ITER1", now=T0)
    i2 = es.create_iteration(t["id"], "b.mp4", 1, "u", now=T0)
    es.complete_iteration(i2["id"], "ITER2", now=T0)
    if uploading:
        es.create_iteration(t["id"], "c.mp4", 1, "u", now=T0)  # still uploading, no file id
    return t


def _req(id):
    return db.get().execute("SELECT * FROM delete_requests WHERE id=?", (id,)).fetchone()


def _files(request_id):
    return [dict(r) for r in db.get().execute(
        "SELECT * FROM delete_files WHERE request_id=? ORDER BY id", (request_id,))]


def test_confirm_must_match_name_exactly():
    p = _project()
    for wrong in ("vod-1020", "VOD-1020.mp4", "vod-1020.mp4x", ""):
        with pytest.raises(deletions.ConfirmMismatch):
            deletions.request_delete("vod", p["id"], wrong, username="boss", role="admin", now=T0)
    assert es.get_project(p["id"])["vod_status"] == "ready"
    r = deletions.request_delete("vod", p["id"], "  vod-1020.mp4 ", username="boss", role="admin", now=T0)
    assert r["delete_at"] == T0 + 3600 and r["label"] == "vod-1020.mp4"
    assert es.get_project(p["id"])["vod_status"] == "deleting"
    with pytest.raises(ValueError, match="Already scheduled"):
        deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)


def test_editor_may_only_delete_downloaded_vod():
    p = _project(downloaded=False)
    with pytest.raises(deletions.NotAllowed):
        deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="ed", role="editor", now=T0)
    es.record_download_click(p["id"], now=T0)
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="ed", role="editor", now=T0)
    assert _req(r["request_id"])["requested_by"] == "ed"


def test_editor_cannot_delete_tape_files_or_project():
    p = _project()
    t = _tape_with_iterations(p["id"])
    with pytest.raises(deletions.NotAllowed):
        deletions.request_delete("tape_files", t["id"], "Ace clip", username="ed", role="editor", now=T0)
    with pytest.raises(deletions.NotAllowed):
        deletions.request_delete("project", p["id"], "Ranked grind", username="ed", role="editor", now=T0)
    with pytest.raises(deletions.NotAllowed):
        deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="x", role="viewer", now=T0)


@pytest.mark.asyncio
async def test_nothing_deleted_before_the_hour(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 3599)
    assert drive.calls == []
    assert _req(r["request_id"])["status"] == "pending"
    await deletions.run_due(T0 + 3600)
    assert drive.calls == ["VODFILE"]
    assert _req(r["request_id"])["status"] == "done"
    assert es.get_project(p["id"])["vod_status"] == "deleted"
    kinds = [e["kind"] for e in es.events_since(0)]
    assert kinds == ["delete_scheduled", "delete_done"]


@pytest.mark.asyncio
async def test_undo_restores_statuses_and_blocks_deletion(drive):
    p = _project()
    t = _tape_with_iterations(p["id"])
    r = deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0)
    assert [i["status"] for i in es.list_iterations(t["id"])] == ["deleting", "deleting"]
    assert [f["drive_file_id"] for f in _files(r["request_id"])] == ["ITER1", "ITER2"]
    deletions.undo(r["request_id"], username="boss", role="admin", now=T0 + 10)
    assert [i["status"] for i in es.list_iterations(t["id"])] == ["ready", "ready"]
    assert _req(r["request_id"])["status"] == "cancelled"
    assert deletions.pending(now=T0 + 10) == []
    await deletions.run_due(T0 + 7200)
    assert drive.calls == []
    with pytest.raises(ValueError):
        deletions.undo(r["request_id"], username="boss", role="admin", now=T0 + 20)
    # a fresh request is allowed once the old one is cancelled
    deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0 + 30)
    assert "delete_undone" in [e["kind"] for e in es.events_since(0)]


@pytest.mark.asyncio
async def test_undo_only_while_pending(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    drive.fail = gdrive.DriveNotConnected()
    await deletions.run_due(T0 + 3600)
    assert _req(r["request_id"])["status"] == "running"
    with pytest.raises(ValueError):
        deletions.undo(r["request_id"], username="boss", role="admin", now=T0 + 3601)


def test_editor_cannot_undo_admins_request():
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    with pytest.raises(deletions.NotAllowed):
        deletions.undo(r["request_id"], username="ed", role="editor", now=T0 + 1)
    assert es.get_project(p["id"])["vod_status"] == "deleting"
    # but the editor can undo his own, and the admin can undo anything
    deletions.undo(r["request_id"], username="boss", role="admin", now=T0 + 2)
    r2 = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="ed", role="editor", now=T0 + 3)
    deletions.undo(r2["request_id"], username="ed", role="editor", now=T0 + 4)
    assert es.get_project(p["id"])["vod_status"] == "ready"


@pytest.mark.asyncio
async def test_boot_grace_gives_a_full_hour_after_restart(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    boot = T0 + 3600 + 600  # it was due 10 minutes before the restart
    assert deletions.apply_boot_grace(boot) == 1
    assert _req(r["request_id"])["delete_at"] == boot + 3600
    [row] = deletions.pending(now=boot)
    assert row["remaining_seconds"] == 3600
    await deletions.run_due(boot + 3599)
    assert drive.calls == []
    await deletions.run_due(boot + 3600)
    assert drive.calls == ["VODFILE"]


@pytest.mark.asyncio
async def test_retry_after_crash_treats_404_as_done(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    # power cut mid-delete: request running, file row left 'deleting'
    with db.transaction() as conn:
        conn.execute("UPDATE delete_requests SET status='running' WHERE id=?", (r["request_id"],))
        conn.execute("UPDATE delete_files SET status='deleting' WHERE request_id=?", (r["request_id"],))
    boot = T0 + 4000
    deletions.apply_boot_grace(boot)
    assert _files(r["request_id"])[0]["status"] == "pending"
    await deletions.run_due(boot)
    assert drive.calls == ["VODFILE"]
    assert _files(r["request_id"])[0]["status"] == "deleted"
    assert _req(r["request_id"])["status"] == "done"
    assert es.get_project(p["id"])["vod_status"] == "deleted"


@pytest.mark.asyncio
async def test_drive_not_connected_keeps_pending_with_backoff(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    drive.fail = gdrive.DriveNotConnected("no token")
    now = T0 + 3600
    await deletions.run_due(now)
    [f] = _files(r["request_id"])
    assert f["status"] == "pending" and f["attempts"] == 1 and f["last_error"]
    first_next = f["next_attempt_at"]
    assert first_next > now
    await deletions.run_due(now + 1)  # not yet due for retry
    assert len(drive.calls) == 1
    drive.fail = gdrive.DriveError("boom", 500)
    await deletions.run_due(first_next)
    [f] = _files(r["request_id"])
    assert f["status"] == "pending" and f["attempts"] == 2
    assert f["next_attempt_at"] - first_next > first_next - now
    assert es.get_project(p["id"])["vod_status"] == "deleting"
    drive.fail = None
    await deletions.run_due(f["next_attempt_at"])
    assert _req(r["request_id"])["status"] == "done"


@pytest.mark.asyncio
async def test_tape_files_marks_iterations_deleted_and_keeps_tape(drive):
    p = _project()
    t = _tape_with_iterations(p["id"])
    r = deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 3600)
    assert sorted(drive.calls) == ["ITER1", "ITER2"]
    assert [i["status"] for i in es.list_iterations(t["id"])] == ["deleted", "deleted"]
    tape = es.get_tape(t["id"])
    assert tape is not None and tape["files_deleted_at"] == T0 + 3600
    assert _req(r["request_id"])["status"] == "done"
    assert es.get_project(p["id"])["vod_status"] == "ready"
    # nothing left: a second request is refused rather than "completing" an empty delete
    with pytest.raises(ValueError, match="Nothing to delete"):
        deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0 + 4000)


@pytest.mark.asyncio
async def test_project_deletes_folder_and_marks_project_deleted(drive):
    p = _project()
    t = _tape_with_iterations(p["id"])
    deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 3600)
    assert drive.calls == ["PFOLDER", "VODFILE", "ITER1", "ITER2"]
    row = es.get_project(p["id"])
    assert row["deleted_at"] == T0 + 3600 and row["vod_status"] == "deleted"
    assert [i["status"] for i in es.list_iterations(t["id"])] == ["deleted", "deleted"]
    assert es.list_projects() == []


@pytest.mark.asyncio
async def test_project_without_folder_still_deletes_its_files(drive):
    p = es.create_project("No folder", "2026-10-21", "t", "v.mp4", 1, now=T0)
    es.mark_vod_ready(p["id"], "VODONLY", now=T0)
    deletions.request_delete("project", p["id"], "No folder", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 3600)
    assert drive.calls == ["VODONLY"]
    assert es.get_project(p["id"])["vod_status"] == "deleted"


@pytest.mark.asyncio
async def test_project_failed_vod_delete_is_not_marked_deleted(drive, monkeypatch):
    p = _project()

    async def fail_vod(file_id):
        drive.calls.append(file_id)
        if file_id == "VODFILE":
            raise gdrive.DriveError("boom", 500)

    monkeypatch.setattr(gdrive, "delete_file", fail_vod)
    r = deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 3600)
    assert _req(r["request_id"])["status"] == "running"
    row = es.get_project(p["id"])
    assert row["vod_status"] == "deleting" and row["deleted_at"] is None


def test_empty_label_cannot_be_confirmed():
    p = es.create_project("", "2026-10-21", "t", "v.mp4", 1, now=T0)
    es.mark_vod_ready(p["id"], "V", now=T0)
    with pytest.raises(ValueError, match="Nothing to confirm against"):
        deletions.request_delete("project", p["id"], "", username="boss", role="admin", now=T0)


def test_tape_files_refused_while_an_upload_is_running():
    p = _project()
    t = _tape_with_iterations(p["id"], uploading=True)
    with pytest.raises(ValueError, match="Wait for the upload to finish first"):
        deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0)
    assert [i["status"] for i in es.list_iterations(t["id"])] == ["ready", "ready", "uploading"]


@pytest.mark.asyncio
async def test_first_sane_run_applies_boot_grace(drive):
    real_now = T0 + 50 * 60
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0 - 50 * 60)
    # due at T0+10min; the process booted with a wrong clock, so no grace was applied at boot
    deletions._reset_for_tests()
    await deletions.run_due(real_now)
    assert drive.calls == []
    assert _req(r["request_id"])["delete_at"] == real_now + 3600
    deletions._prev_now = None  # the 30 s ticks in between are not simulated
    await deletions.run_due(real_now + 3600)
    assert drive.calls == ["VODFILE"]


@pytest.mark.asyncio
async def test_run_due_never_raises(drive, monkeypatch):
    p = _project()
    deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    drive.fail = RuntimeError("unexpected")
    await deletions.run_due(T0 + 3600)  # must not raise
    [f] = _files(1)
    assert f["status"] == "pending" and f["attempts"] == 1


@pytest.mark.asyncio
async def test_forward_clock_jump_gives_a_fresh_hour(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 30)
    jumped = T0 + 30 + 4000  # 30 s tick, but the clock moved 4000 s: past the delete's due time
    await deletions.run_due(jumped)
    assert drive.calls == []
    assert _req(r["request_id"])["delete_at"] == jumped + 3600
    await deletions.run_due(jumped + 30)
    assert drive.calls == []


def test_clamp_after_clock_reset_pulls_future_deletes_back():
    p = _project()
    t = _tape_with_iterations(p["id"])
    future = T0 + 10 * 86400  # requested while the clock ran ten days fast
    far = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=future)
    near = deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0 - 600)
    assert deletions.clamp_after_clock_reset(T0) == 1
    assert _req(far["request_id"])["delete_at"] == T0 + 3600
    assert _req(near["request_id"])["delete_at"] == T0 + 3000
    assert all(f["next_attempt_at"] <= T0 + 3600 for f in _files(far["request_id"]))


def test_project_delete_refused_while_an_upload_is_running():
    p = _project()
    _tape_with_iterations(p["id"], uploading=True)
    with pytest.raises(ValueError, match="Wait for the upload to finish first"):
        deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    assert deletions.pending(now=T0) == []


def test_project_delete_refused_while_its_vod_is_uploading():
    p = es.create_project("Ranked grind", "2026-10-20", "t", "vod-1020.mp4", 100, now=T0)
    with pytest.raises(ValueError, match="Wait for the VOD upload to finish first"):
        deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    assert deletions.pending(now=T0) == []


def test_overlapping_project_and_child_deletes_are_refused():
    p = _project()
    t = _tape_with_iterations(p["id"])
    r = deletions.request_delete("tape_files", t["id"], "Ace clip", username="boss", role="admin", now=T0)
    with pytest.raises(ValueError, match="Another delete for this is already scheduled"):
        deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    deletions.undo(r["request_id"], username="boss", role="admin", now=T0)
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    with pytest.raises(ValueError, match="Another delete for this is already scheduled"):
        deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    deletions.undo(r["request_id"], username="boss", role="admin", now=T0)
    deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    for kind, tid, label in (("vod", p["id"], "vod-1020.mp4"), ("tape_files", t["id"], "Ace clip")):
        with pytest.raises(ValueError, match="Another delete for this is already scheduled"):
            deletions.request_delete(kind, tid, label, username="boss", role="admin", now=T0)


def test_project_being_deleted_check():
    p = _project()
    deletions.ensure_project_not_deleting(p["id"])  # nothing scheduled: fine
    r = deletions.request_delete("project", p["id"], "Ranked grind", username="boss", role="admin", now=T0)
    with pytest.raises(ValueError, match="This project is being deleted"):
        deletions.ensure_project_not_deleting(p["id"])
    deletions.undo(r["request_id"], username="boss", role="admin", now=T0)
    deletions.ensure_project_not_deleting(p["id"])


def test_project_delete_refused_for_clip_holding_combined_reel(drive):
    from editor import editor_service as svc
    a = es.create_project("Clip A", "2026-10-20", "", "a.mp4", 1, now=T0, duration_s=20.0)
    b = es.create_project("Clip B", "2026-10-20", "", "b.mp4", 1, now=T0, duration_s=20.0)
    es.mark_vod_ready(a["id"], "VA", now=T0)
    es.mark_vod_ready(b["id"], "VB", now=T0)
    reel = svc.combine_clips([a["id"], b["id"]], "Duo", "aryan")
    it = es.create_iteration(reel["id"], "duo.mp4", 1, "u", now=T0)
    es.complete_iteration(it["id"], "DUO1", now=T0)
    with pytest.raises(ValueError, match="This clip holds a combined reel - delete the reel's files first"):
        deletions.request_delete("project", a["id"], "Clip A", username="aryan", role="admin", now=T0)
    # The other source holds no reel of its own and is deleted normally.
    deletions.request_delete("project", b["id"], "Clip B", username="aryan", role="admin", now=T0)
    deletions.request_delete("tape_files", reel["id"], "Duo", username="aryan", role="admin", now=T0)
    import asyncio
    asyncio.run(deletions.run_due(T0 + deletions.DELETE_GRACE_SECONDS + 1))
    assert es.get_tape(reel["id"])["files_deleted_at"] is not None
    assert deletions.request_delete("project", a["id"], "Clip A", username="aryan", role="admin",
                                    now=T0 + 7200)["label"] == "Clip A"


def test_empty_tape_files_delete_is_refused(drive):
    # An empty "files delete" would set files_deleted_at and unlock the host clip's project delete
    # while the combined reel is still the only reel of its other clips.
    p = _project()
    t = es.create_tape(p["id"], "Empty", now=T0)
    with pytest.raises(ValueError, match="Nothing to delete"):
        deletions.request_delete("tape_files", t["id"], "Empty", username="boss", role="admin", now=T0)
    assert deletions.pending(now=T0) == [] and es.get_tape(t["id"])["files_deleted_at"] is None


@pytest.mark.asyncio
async def test_slow_tick_is_not_a_clock_jump(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 30, mono=1000.0)
    await deletions.run_due(T0 + 930, mono=1900.0)  # loop stalled 900 s: wall and monotonic agree
    assert _req(r["request_id"])["delete_at"] == T0 + 3600


@pytest.mark.asyncio
async def test_wall_jump_without_monotonic_is_a_jump(drive):
    p = _project()
    r = deletions.request_delete("vod", p["id"], "vod-1020.mp4", username="boss", role="admin", now=T0)
    await deletions.run_due(T0 + 30, mono=1000.0)
    await deletions.run_due(T0 + 930, mono=1030.0)  # wall +900, monotonic +30
    assert _req(r["request_id"])["delete_at"] == T0 + 930 + 3600
