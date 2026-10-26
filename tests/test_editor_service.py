import pytest

import config
import db
import editor_service as svc
import editor_store as es
import gdrive
import gdrive_oauth


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
    for bad in ({"parents": ["other"]}, {"appProperties": {"iteration_id": "99"}}, {"size": 11}):
        _env["file"] = {**_env["file"], **bad}
        with pytest.raises(ValueError, match="not this iteration"):
            await svc.complete_iteration_upload(iid, "fid", "x")


@pytest.mark.asyncio
async def test_complete_twice_carries_over_once_and_logs_one_event(_env, monkeypatch):
    import editor_comments
    calls = []
    monkeypatch.setattr(editor_comments, "carry_over", lambda i: calls.append(i))
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    r = await svc.complete_iteration_upload(iid, "fid", "x")
    assert r["status"] == "ready" and calls == [iid]
    n = db.get().execute("SELECT COUNT(*) FROM events WHERE kind='iteration_ready'").fetchone()[0]
    assert n == 1


@pytest.mark.asyncio
async def test_vod_ready_shares_with_editor_email_when_set(_env, monkeypatch):
    p, _ = _tape()
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
    await svc.vod_ready(p["id"], "vf", "sys")
    _, iid = await _started(_env)
    await svc.complete_iteration_upload(iid, "fid", "x")
    v = await svc.storage_view()
    assert sorted(f["label"] for f in v["files"]) == ["Ace — VOD", "Cut v1"]
    assert v["pending_deletes"] == [] and v["quota"]["free"] == 1000
