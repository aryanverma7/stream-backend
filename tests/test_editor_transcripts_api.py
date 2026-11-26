import pytest

from core import db
from editor import editor_service as svc
from editor import editor_store as es
from editor import transcripts as tr
from tests.test_editor_api import _world, api, drive  # noqa: F401 - drive is the autouse fixture

SEGS = [{"start": 0, "end": 2, "text": "ye clutch dekho"}, {"start": 2, "end": 4, "text": "too long bhai"}]


@pytest.mark.asyncio
async def test_both_roles_read_and_flip_the_switch():
    async with api() as a:
        assert await a.admin("GET", "/api/editor/transcriber") == (
            200, {"enabled": False, "auto": False, "last_seen": None, "queued": 0, "running": 0})
        assert (await a.editor("PUT", "/api/editor/transcriber", {"enabled": True}))[1]["enabled"] is True
        status, got = await a.admin("PUT", "/api/editor/transcriber", {"auto": True})
        assert status == 200 and (got["enabled"], got["auto"]) == (True, True)
        assert (await a.editor("GET", "/api/editor/transcriber"))[1]["auto"] is True
        assert (await a.admin("PUT", "/api/editor/transcriber", {"enabled": "yes"}))[0] == 400
        tr.set_settings(False, None)
        db.meta_set("transcriber_last_seen", "123.5")
        assert (await a.editor("GET", "/api/editor/transcriber"))[1]["last_seen"] == 123.5


@pytest.mark.asyncio
async def test_request_and_read_transcript():
    p, t, it, c = _world()
    path = f"/api/editor/iterations/{it['id']}/transcript"
    async with api() as a:
        assert await a.editor("GET", path) == (200, {"status": "none", "segments": None, "error": None})
        status, row = await a.editor("POST", path)
        assert status == 200 and row["status"] == "queued" and row["requested_by"] == "cutguy"
        assert (await a.admin("GET", "/api/editor/transcriber"))[1]["queued"] == 1
        job = tr.claim()
        assert (await a.admin("GET", path))[1]["status"] == "running"
        tr.complete(it["id"], job["token"], SEGS, "m")
        assert await a.admin("GET", path) == (200, {"status": "done", "segments": tr.get(it["id"])["segments"],
                                                    "error": None})
        assert (await a.admin("GET", "/api/editor/iterations/999/transcript"))[0] == 404


@pytest.mark.asyncio
async def test_request_on_unready_version_is_409():
    p, t, it, c = _world()
    up = es.create_iteration(t["id"], "b.mp4", 10, "")
    async with api() as a:
        assert (await a.admin("POST", f"/api/editor/iterations/{up['id']}/transcript"))[0] == 409


@pytest.mark.asyncio
async def test_search_returns_transcript_hit_with_start():
    p, t, it, c = _world()
    tr.request(it["id"], "aryan")
    tr.complete(it["id"], tr.claim()["token"], SEGS, "m")
    async with api() as a:
        hits = (await a.editor("GET", "/api/editor/search?q=too%20long"))[1]["hits"]
    kinds = [h["kind"] for h in hits]
    assert "comment" in kinds and "transcript" in kinds
    hit = next(h for h in hits if h["kind"] == "transcript")
    assert (hit["iteration_id"], hit["edit_id"], hit["project_id"], hit["start"], hit["text"]) == (
        it["id"], t["id"], p["id"], 2.0, "too long bhai")


@pytest.mark.asyncio
async def test_new_version_queued_only_when_auto(drive):
    p, t, it, c = _world()
    drive["files"]["F2"] = {"parents": ["TF"], "appProperties": {"iteration_id": None}, "size": 10}
    for auto, expect in ((False, None), (True, "queued")):
        tr.set_settings(None, auto)
        nxt = es.create_iteration(t["id"], "c.mp4", 10, "")
        drive["files"]["F2"]["appProperties"]["iteration_id"] = str(nxt["id"])
        await svc.complete_iteration_upload(nxt["id"], "F2", "cutguy")
        got = tr.get(nxt["id"])
        assert (got and got["status"]) == expect


@pytest.mark.asyncio
async def test_fix_a_line_both_roles_searchable_and_kept_on_rerequest():
    p, t, it, c = _world()
    tr.request(it["id"], "aryan")
    tr.complete(it["id"], tr.claim()["token"], SEGS, "m")
    path = f"/api/editor/iterations/{it['id']}/transcript/lines"
    async with api() as a:
        status, got = await a.editor("PUT", f"{path}/1", {"text": "  bahut lamba hai bhai  "})
        assert status == 200 and got["segments"][1] == {"start": 2.0, "end": 4.0, "text": "bahut lamba hai bhai",
                                                         "edited": True}
        assert got["segments"][0]["text"] == "ye clutch dekho" and "edited" not in got["segments"][0]
        assert (await a.admin("PUT", f"{path}/0", {"text": "ye clutch dekho yaar"}))[0] == 200
        hits = (await a.admin("GET", "/api/editor/search?q=lamba"))[1]["hits"]
        assert any(h["kind"] == "transcript" for h in hits)
        for bad in ({"text": ""}, {"text": "   "}, {"text": "x" * 2001}, {"text": 5}, {}):
            assert (await a.admin("PUT", f"{path}/0", bad))[0] == 400
        assert (await a.admin("PUT", f"{path}/2", {"text": "x"}))[0] == 400  # no third line
        assert (await a.admin("POST", f"/api/editor/iterations/{it['id']}/transcript"))[1]["status"] == "done"
    assert tr.get(it["id"])["segments"][1]["text"] == "bahut lamba hai bhai"


@pytest.mark.asyncio
async def test_fix_a_line_before_done_is_409():
    p, t, it, c = _world()
    path = f"/api/editor/iterations/{it['id']}/transcript/lines/0"
    async with api() as a:
        assert (await a.admin("PUT", path, {"text": "x"}))[0] == 409  # never requested
        tr.request(it["id"], "aryan")
        assert (await a.admin("PUT", path, {"text": "x"}))[0] == 409  # queued
