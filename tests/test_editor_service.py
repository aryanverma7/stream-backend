import pytest

from core import config
from core import db
from editor import editor_service as svc
from editor import editor_store as es
from editor import gdrive
from editor import gdrive_oauth


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config.config, "_data", {"public_base_url": "https://x.example"})
    db.meta_set("gdrive_projects", "PROJ")
    s = {"folders": [], "started": [], "shared": [], "quota": {"limit": 1000, "usage": 0}, "file": {}}
    s_ = s

    async def about():
        return s_["quota"]

    async def create_folder(name, parent):
        await __import__("asyncio").sleep(0)
        s_["folders"].append((name, parent))
        return f"F{len(s_['folders'])}"

    async def start(name, parent, size, mime, props, origin):
        s_["started"].append((name, parent, size, mime, props, origin))
        return "https://upload/s"

    async def get_file(fid):
        return s_["file"]

    async def share(fid, email, role="reader"):
        s_["shared"].append((fid, email, role))

    monkeypatch.setattr(gdrive, "about", about)
    monkeypatch.setattr(gdrive, "create_folder", create_folder)
    monkeypatch.setattr(gdrive, "start_resumable_upload", start)
    monkeypatch.setattr(gdrive, "get_file", get_file)
    monkeypatch.setattr(gdrive, "share_with", share)
    return s


def _tape():
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    return p, es.create_tape(p["id"], "Cut", now=1.0)


@pytest.mark.asyncio
async def test_start_upload_refuses_when_drive_too_full(_env):
    _, t = _tape()
    _env["quota"] = {"limit": 1000, "usage": 900}
    with pytest.raises(svc.DriveFull) as e:
        await svc.start_iteration_upload(t["id"], "a.mp4", 500, "video/mp4")
    assert (e.value.needed, e.value.free) == (500, 100)


@pytest.mark.asyncio
async def test_start_upload_names_file_and_sets_app_properties(_env):
    _, t = _tape()
    r = await svc.start_iteration_upload(t["id"], "a.mp4", 10, "video/mp4")
    name, parent, size, mime, props, origin = _env["started"][0]
    assert name == "v1 - a.mp4" and parent == "F2" and props == {"iteration_id": str(r["iteration_id"])}
    assert origin == "https://x.example" and r["upload_url"] == "https://upload/s"
    assert es.get_iteration(r["iteration_id"])["upload_url"] == "https://upload/s"


async def _started(_env):
    _, t = _tape()
    r = await svc.start_iteration_upload(t["id"], "a.mp4", 10, "video/mp4")
    _env["file"] = {"parents": ["F2"], "appProperties": {"iteration_id": str(r["iteration_id"])}, "size": 10}
    return t, r["iteration_id"]


@pytest.mark.asyncio
async def test_complete_rejects_a_foreign_file(_env):
    _, iid = await _started(_env)
    base = dict(_env["file"])
    for bad in ({"parents": ["other"]}, {"appProperties": {"iteration_id": "99"}}, {"size": 11}):
        _env["file"] = {**base, **bad}
        with pytest.raises(ValueError, match="not this iteration"):
            await svc.complete_iteration_upload(iid, "fid", "x")


@pytest.mark.asyncio
async def test_complete_twice_logs_one_event_and_carry_over_is_rerun(_env, monkeypatch):
    from editor import editor_comments
    calls = []
    monkeypatch.setattr(editor_comments, "carry_over", lambda i: calls.append(i))
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    r = await svc.complete_iteration_upload(iid, "fid", "x")
    assert r["status"] == "ready" and calls == [iid, iid]  # carry_over is idempotent; re-running is the retry path
    n = db.get().execute("SELECT COUNT(*) FROM events WHERE kind='iteration_ready'").fetchone()[0]
    assert n == 1


def _vod_file(_env, p, **over):
    """The Drive file the uploader would have made for project p (vod_size 5), folder recorded on the row."""
    es._set("projects", p["id"], None, drive_folder_id="PF")
    _env["file"] = {"parents": ["PF"], "appProperties": {"project_id": str(p["id"])}, "size": 5, **over}


@pytest.mark.asyncio
async def test_vod_ready_rejects_file_outside_project_folder(_env):
    p, _ = _tape()
    _vod_file(_env, p, parents=["other"])
    with pytest.raises(ValueError, match="not this project's upload"):
        await svc.vod_ready(p["id"], "vf", "agent")
    assert es.get_project(p["id"])["vod_status"] == "uploading" and _env["shared"] == []


@pytest.mark.asyncio
async def test_vod_ready_rejects_wrong_project_property(_env):
    p, _ = _tape()
    _vod_file(_env, p, appProperties={"project_id": "99"})
    with pytest.raises(ValueError, match="not this project's upload"):
        await svc.vod_ready(p["id"], "vf", "agent")
    assert es.get_project(p["id"])["vod_status"] == "uploading" and _env["shared"] == []


@pytest.mark.asyncio
async def test_vod_ready_rejects_wrong_size(_env):
    p, _ = _tape()
    _vod_file(_env, p, size=6)
    with pytest.raises(ValueError, match="not this project's upload"):
        await svc.vod_ready(p["id"], "vf", "agent")
    assert es.get_project(p["id"])["vod_status"] == "uploading" and _env["shared"] == []


@pytest.mark.asyncio
async def test_vod_ready_accepts_matching_file_and_records_agent_event(_env):
    p, _ = _tape()
    _vod_file(_env, p)
    assert (await svc.vod_ready(p["id"], "vf", "agent"))["vod_status"] == "ready"
    row = db.get().execute("SELECT actor FROM events WHERE kind='vod_ready'").fetchone()
    assert row[0] == "agent"


@pytest.mark.asyncio
async def test_vod_ready_repeat_makes_no_drive_call(_env, monkeypatch):
    p, _ = _tape()
    _vod_file(_env, p)
    await svc.vod_ready(p["id"], "vf", "agent")

    async def boom(fid):
        raise AssertionError("Drive was called")

    monkeypatch.setattr(gdrive, "get_file", boom)
    assert (await svc.vod_ready(p["id"], "vf", "agent"))["vod_status"] == "ready"


@pytest.mark.asyncio
async def test_vod_ready_shares_with_editor_email_when_set(_env, monkeypatch):
    p, _ = _tape()
    _vod_file(_env, p)
    monkeypatch.setitem(config.config._data, "editor_google_email", "e@x.com")
    await svc.vod_ready(p["id"], "vf", "sys")
    await svc.vod_ready(p["id"], "vf", "sys")
    assert _env["shared"] == [("vf", "e@x.com", "reader")]
    assert es.get_project(p["id"])["vod_status"] == "ready"
    assert db.get().execute("SELECT COUNT(*) FROM events WHERE kind='vod_ready'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_folders_created_once(_env):
    _, t = _tape()
    a = await svc.ensure_tape_folder(t["id"])
    b = await svc.ensure_tape_folder(t["id"])
    assert a == b and _env["folders"] == [("2026-10-20 Ace", "PROJ"), ("Cut", "F1")]


@pytest.mark.asyncio
async def test_media_link_gives_token_not_url_and_expiry(_env, monkeypatch):
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    async def tok(now=None): return "TOK"
    monkeypatch.setattr(gdrive, "access_token", tok)
    monkeypatch.setattr(gdrive, "token_expires_at", lambda: __import__("time").time() + 100)
    _env["file"] = {**_env["file"], "webViewLink": "W"}
    r = await svc.media_link(iteration_id=iid)
    assert r["access_token"] == "TOK" and "TOK" not in r["url"] and 90 < r["expires_in"] <= 100
    assert r["url"].endswith("/files/fid?alt=media") and r["drive_url"] == "W"


def test_token_expires_at_getter(monkeypatch):
    monkeypatch.setattr(gdrive, "_token_expires_at", 42.0)
    assert gdrive.token_expires_at() == 42.0


@pytest.mark.asyncio
async def test_storage_view_lists_ready_and_deleting(_env):
    p, _ = _tape()
    _vod_file(_env, p)
    await svc.vod_ready(p["id"], "vf", "sys")
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    v = await svc.storage_view()
    assert sorted(f["label"] for f in v["files"]) == ["Ace — VOD", "Cut v1"]
    names = {f["kind"]: (f["project_name"], f["vod_filename"], f["tape_name"]) for f in v["files"]}
    assert names["vod"][0] == "Ace" and names["vod"][1] == p["vod_filename"] and names["vod"][2] is None
    assert names["iteration"] == ("Ace", None, "Cut")
    assert v["pending_deletes"] == [] and v["quota"]["free"] == 1000


@pytest.mark.asyncio
async def test_concurrent_tape_folder_creates_once(_env):
    import asyncio
    _, t = _tape()
    a, b = await asyncio.gather(svc.ensure_tape_folder(t["id"]), svc.ensure_tape_folder(t["id"]))
    assert a == b and len(_env["folders"]) == 2  # project + tape, once each


@pytest.mark.asyncio
async def test_failed_session_start_leaves_no_row_and_frees_version(_env, monkeypatch):
    _, t = _tape()
    async def boom(*a, **k): raise gdrive.DriveError("x", 500)
    monkeypatch.setattr(gdrive, "start_resumable_upload", boom)
    with pytest.raises(gdrive.DriveError):
        await svc.start_iteration_upload(t["id"], "a.mp4", 10, "video/mp4")
    assert es.list_iterations(t["id"]) == []
    async def ok(*a, **k): return "u"
    monkeypatch.setattr(gdrive, "start_resumable_upload", ok)
    r = await svc.start_iteration_upload(t["id"], "a.mp4", 10, "video/mp4")
    assert r["version"] == 1


@pytest.mark.asyncio
async def test_refused_upload_makes_no_folder_or_row(_env):
    _, t = _tape()
    _env["quota"] = {"limit": 10, "usage": 10}
    with pytest.raises(svc.DriveFull):
        await svc.start_iteration_upload(t["id"], "a.mp4", 5, "video/mp4")
    assert _env["folders"] == [] and es.list_iterations(t["id"]) == []


@pytest.mark.asyncio
async def test_missing_tape_is_value_error(_env):
    with pytest.raises(ValueError):
        await svc.start_iteration_upload(999, "a.mp4", 5, "video/mp4")


@pytest.mark.asyncio
async def test_vod_ready_refuses_deleting_vod(_env):
    p, _ = _tape()
    es._set("projects", p["id"], None, vod_status="deleting")
    with pytest.raises(ValueError, match="being deleted"):
        await svc.vod_ready(p["id"], "vf", "sys")


@pytest.mark.asyncio
async def test_complete_with_missing_tape_is_value_error(_env, monkeypatch):
    _, iid = await _started(_env)
    monkeypatch.setattr(es, "get_tape", lambda i: None)
    with pytest.raises(ValueError, match="No such tape"):
        await svc.complete_iteration_upload(iid, "fid", "x")


async def _deleting_project(_env):
    from editor import deletions
    t, iid = await _started(_env)
    v = es.create_iteration(t["id"], "b.mp4", 10, "u")
    es.abandon_iteration(v["id"])  # not 'uploading', so the project delete is allowed
    es.abandon_iteration(iid)
    es.mark_vod_ready(t["project_id"], "VOD0")
    p = es.get_project(t["project_id"])
    deletions.request_delete("project", p["id"], p["name"], username="aryan", role="admin")
    return p, t, iid


@pytest.mark.asyncio
async def test_no_new_work_lands_in_a_project_being_deleted(_env):
    p, t, iid = await _deleting_project(_env)
    with pytest.raises(ValueError, match="This project is being deleted"):
        svc.create_tape(p["id"], "New", "aryan")
    calls = (svc.start_iteration_upload(t["id"], "c.mp4", 10, "video/mp4"),
             svc.complete_iteration_upload(iid, "fid", "x"))
    for c in calls:
        with pytest.raises(ValueError, match="This project is being deleted"):
            await c
    with pytest.raises(ValueError, match="being deleted"):  # the project delete took the VOD's file too
        await svc.vod_ready(p["id"], "VOD", "x")
    assert len(es.list_tapes(p["id"])) == 1 and _env["started"] == [_env["started"][0]] and _env["shared"] == []
    assert es.get_iteration(iid)["status"] == "abandoned"
    assert es.get_project(p["id"])["vod_file_id"] == "VOD0"


@pytest.mark.asyncio
async def test_retry_after_carry_over_crash_carries_over_and_logs_once(_env, monkeypatch):
    from editor import editor_comments
    calls = []

    def flaky(i):
        calls.append(i)
        if len(calls) == 1:
            raise RuntimeError("power cut")

    monkeypatch.setattr(editor_comments, "carry_over", flaky)
    _, iid = await _started(_env)
    with pytest.raises(RuntimeError):
        await svc.complete_iteration_upload(iid, "fid", "x")
    await svc.complete_iteration_upload(iid, "fid", "x")
    assert calls == [iid, iid]
    n = db.get().execute("SELECT COUNT(*) FROM events WHERE kind='iteration_ready'").fetchone()[0]
    assert n == 1


@pytest.mark.asyncio
async def test_ready_and_its_event_commit_together(_env, monkeypatch):
    _, iid = await _started(_env)
    p = es.create_project("B", "2026-10-21", "", "v.mp4", 5)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(es, "record_event", boom)
    with pytest.raises(RuntimeError):
        await svc.complete_iteration_upload(iid, "fid", "x")
    _vod_file(_env, p)
    with pytest.raises(RuntimeError):
        await svc.vod_ready(p["id"], "VOD", "x")
    assert es.get_iteration(iid)["status"] == "uploading"
    assert es.get_project(p["id"])["vod_status"] == "uploading"


@pytest.mark.asyncio
async def test_abandoned_upload_with_verified_file_recovers(_env):
    _, iid = await _started(_env)
    es.abandon_iteration(iid)
    assert (await svc.complete_iteration_upload(iid, "fid", "x"))["status"] == "ready"


# -- clips and reels --

def _clip(name, now=1.0):
    return es.create_project(name, "2026-10-20", "", f"{name}.mp4", 5, now=now, duration_s=30.0)


def _events(kind):
    return [e for e in es.events_since(0) if e["kind"] == kind]


def test_combine_two_clips_replaces_their_reels():
    a, b, c = _clip("A", 1.0), _clip("B", 2.0), _clip("C", 3.0)
    reel = svc.combine_clips([b["id"], a["id"]], "Best of", "cutguy")
    assert (reel["id"], reel["name"], reel["kind"]) == (b["reel_tape_id"], "Best of", "reel")
    assert reel["source_project_ids"] == [a["id"], b["id"]]  # oldest clip first
    assert es.get_tape(a["reel_tape_id"]) is None  # the other reel is gone
    assert es.get_project(a["id"])["reel_tape_id"] == es.get_project(b["id"])["reel_tape_id"] == reel["id"]
    assert es.list_tapes(a["id"]) == [] and es.reel_for_clip(c["id"])["id"] == c["reel_tape_id"]
    [ev] = _events("reels_combined")
    assert (ev["actor"], ev["tape_id"]) == ("cutguy", reel["id"])
    # Extending needs every clip already on the reel; a clip is never in two reels.
    with pytest.raises(ValueError):
        svc.combine_clips([c["id"], a["id"]], "x", "cutguy")
    assert svc.combine_clips([c["id"], a["id"], b["id"]], "All", "aryan")["source_project_ids"] == \
        [a["id"], b["id"], c["id"]]


def test_combine_refused_for_bad_input():
    a, b = _clip("A"), _clip("B")
    vod = es.create_project("V", "2026-10-20", "", "v.mp4", 5, duration_s=4000.0)
    for ids in ([a["id"]], [a["id"], a["id"]], [a["id"], vod["id"]], [a["id"], 999]):
        with pytest.raises(ValueError):
            svc.combine_clips(ids, "x", "aryan")
    db.get().execute("UPDATE projects SET deleted_at=9 WHERE id=?", (b["id"],))
    with pytest.raises(ValueError):
        svc.combine_clips([a["id"], b["id"]], "x", "aryan")
    assert len(es.list_tapes(a["id"])) == 1 and _events("reels_combined") == []


def test_combine_refused_when_a_reel_has_a_version():
    a, b = _clip("A"), _clip("B")
    es.create_iteration(b["reel_tape_id"], "f.mp4", 1, "u")  # even an unfinished upload counts
    with pytest.raises(ValueError, match="version"):
        svc.combine_clips([a["id"], b["id"]], "x", "aryan")
    assert es.reel_for_clip(a["id"])["source_project_ids"] == [a["id"]]


def test_uncombine_restores_own_reels():
    a, b, c = _clip("A", 1.0), _clip("B", 2.0), _clip("C", 3.0)
    reel = svc.combine_clips([a["id"], b["id"], c["id"]], "Trio", "cutguy")
    reels = svc.uncombine(reel["id"], "aryan")
    assert [(r["name"], r["source_project_ids"]) for r in reels] == [("A", [a["id"]]), ("B", [b["id"]]),
                                                                     ("C", [c["id"]])]
    assert reels[0]["id"] == reel["id"] and all(r["kind"] == "reel" for r in reels)
    for clip, r in zip((a, b, c), reels):
        assert r["project_id"] == clip["id"] and es.get_project(clip["id"])["reel_tape_id"] == r["id"]
    [ev] = _events("reels_uncombined")
    assert (ev["actor"], ev["tape_id"]) == ("aryan", reel["id"])
    with pytest.raises(ValueError):
        svc.uncombine(reel["id"], "aryan")  # only one source now
    t = es.create_tape(es.create_project("V", "d", "", "v.mp4", 5)["id"], "plain")
    with pytest.raises(ValueError):
        svc.uncombine(t["id"], "aryan")


def test_uncombine_refused_after_a_version():
    a, b = _clip("A"), _clip("B")
    reel = svc.combine_clips([a["id"], b["id"]], "Duo", "cutguy")
    es.create_iteration(reel["id"], "f.mp4", 1, "u")
    with pytest.raises(ValueError, match="version"):
        svc.uncombine(reel["id"], "aryan")
    assert es.get_tape(reel["id"])["source_project_ids"] == [a["id"], b["id"]]


def test_no_combine_into_a_project_being_deleted():
    from editor import deletions
    a, b = _clip("A"), _clip("B")
    es.mark_vod_ready(b["id"], "VB")
    deletions.request_delete("project", b["id"], "B", username="aryan", role="admin")
    with pytest.raises(ValueError, match="being deleted"):
        svc.combine_clips([a["id"], b["id"]], "x", "aryan")


@pytest.mark.asyncio
async def test_reel_works_like_a_tape(_env):
    from editor import editor_comments as ec
    c = _clip("Medal")
    reel_id = c["reel_tape_id"]
    with pytest.raises(ValueError, match="exactly one reel"):
        svc.create_tape(c["id"], "second", "aryan")
    ids = []
    for _ in range(2):
        r = await svc.start_iteration_upload(reel_id, "r.mp4", 10, "video/mp4")
        _env["file"] = {"parents": [es.get_tape(reel_id)["drive_folder_id"]],
                        "appProperties": {"iteration_id": str(r["iteration_id"])}, "size": 10}
        await svc.complete_iteration_upload(r["iteration_id"], f"fid{r['version']}", "cutguy")
        ids.append(r["iteration_id"])
        if len(ids) == 1:
            com = ec.add_comment(ids[0], "aryan", "fix", "louder")
    assert [i["version"] for i in es.list_iterations(reel_id)] == [1, 2]
    [carried] = ec.comments_for_iteration(ids[1])
    assert carried["id"] == com["id"] and carried["carried"] and es.get_tape(reel_id)["status"] == "in_review"


@pytest.mark.asyncio
async def test_complete_refused_while_tape_files_delete_is_live(_env):
    t, iid = await _started(_env)
    db.get().execute("INSERT INTO delete_requests(kind, target_id, label, requested_by, requested_at, delete_at,"
                     " status) VALUES ('tape_files', ?, 'Cut', 'aryan', 1, 2, 'pending')", (t["id"],))
    with pytest.raises(ValueError, match="This tape's files are being deleted"):
        await svc.complete_iteration_upload(iid, "fid", "x")
    assert es.get_iteration(iid)["status"] == "uploading"


def _cold_restart(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")  # same file, nothing ensured yet: a process restart


def test_create_tape_on_a_cold_start(_env, tmp_path):
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    _cold_restart(tmp_path)
    assert svc.create_tape(p["id"], "Cut", "ed")["name"] == "Cut"


@pytest.mark.asyncio
async def test_vod_ready_on_a_cold_start(_env, tmp_path):
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    _vod_file(_env, p)
    _cold_restart(tmp_path)
    assert (await svc.vod_ready(p["id"], "vf", "system"))["vod_status"] == "ready"


@pytest.mark.asyncio
async def test_completed_version_is_shared_with_the_editor(_env, monkeypatch):
    monkeypatch.setitem(config.config._data, "editor_google_email", "e@x.com")
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    assert _env["shared"] == [("fid", "e@x.com", "reader")]


@pytest.mark.asyncio
async def test_no_share_without_an_editor_email(_env):
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    assert _env["shared"] == []


@pytest.mark.asyncio
async def test_a_failed_share_does_not_fail_completion(_env, monkeypatch):
    monkeypatch.setitem(config.config._data, "editor_google_email", "e@x.com")
    _, iid = await _started(_env)

    async def boom(*a, **k):
        raise gdrive.DriveError("nope", 500)
    monkeypatch.setattr(gdrive, "share_with", boom)
    assert (await svc.complete_iteration_upload(iid, "fid", "x"))["status"] == "ready"


@pytest.mark.asyncio
async def test_renewing_a_session_restarts_the_abandon_clock(_env):
    _, iid = await _started(_env)
    db.get().execute("UPDATE iterations SET status='abandoned', created_at=1.0, updated_at=1.0 WHERE id=?", (iid,))
    assert await svc.renew_iteration_upload(iid, "video/mp4") == "https://upload/s"
    assert es.get_iteration(iid)["updated_at"] > 1_000_000


@pytest.mark.asyncio
async def test_no_new_version_on_a_tape_whose_files_are_being_or_were_deleted(_env, monkeypatch):
    from editor import deletions
    t, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    deletions.request_delete("tape_files", t["id"], t["name"], username="aryan", role="admin")
    with pytest.raises(ValueError, match="being deleted"):
        await svc.start_iteration_upload(t["id"], "b.mp4", 10, "video/mp4")
    db.get().execute("UPDATE delete_requests SET status='done'")
    db.get().execute("UPDATE tapes SET files_deleted_at=5 WHERE id=?", (t["id"],))
    with pytest.raises(ValueError, match="files were deleted"):
        await svc.start_iteration_upload(t["id"], "b.mp4", 10, "video/mp4")
    assert len(es.list_iterations(t["id"])) == 1


@pytest.mark.asyncio
async def test_a_tape_delete_requested_during_the_awaits_is_seen_after_them(_env, monkeypatch):
    from editor import deletions
    t, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    real = svc.free_space

    async def racing():
        deletions.request_delete("tape_files", t["id"], t["name"], username="aryan", role="admin")
        return await real()
    monkeypatch.setattr(svc, "free_space", racing)
    with pytest.raises(ValueError, match="being deleted"):
        await svc.start_iteration_upload(t["id"], "b.mp4", 10, "video/mp4")
    assert len(es.list_iterations(t["id"])) == 1


@pytest.mark.asyncio
async def test_a_failed_vod_share_still_completes(_env, monkeypatch):
    monkeypatch.setitem(config.config._data, "editor_google_email", "e@x.com")
    p, _ = _tape()
    _vod_file(_env, p)

    async def boom(*a, **k):
        raise gdrive.DriveError("nope", 500)
    monkeypatch.setattr(gdrive, "share_with", boom)
    assert (await svc.vod_ready(p["id"], "vf", "agent"))["vod_status"] == "ready"
