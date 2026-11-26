import pytest

from core import db
from editor import deletions
from editor import editor_store as es
from editor import gdrive
from editor import transcripts as tr


@pytest.fixture(autouse=True)
def _db(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")
    deletions._reset_for_tests()


def _edit():
    p = es.create_project("P", "2026-10-20", "", "v.mp4", 1, now=1.0)
    return es.create_edit(p["id"], "t", now=1.0)


def _ready(edit_id=None):
    it = es.create_iteration(edit_id or _edit()["id"], "f.mp4", 1, "u", now=1.0)
    return es.complete_iteration(it["id"], f"fid{it['id']}", now=2.0)[0]


SEGS = [{"start": 0, "end": 1.5, "text": "ye clutch dekho"}, {"start": 1.5, "end": 3, "text": "GG bhai"}]


def test_defaults_are_paused_and_manual():
    assert tr.settings() == {"enabled": False, "auto": False}
    assert tr.set_settings(True, None) == {"enabled": True, "auto": False}
    assert tr.set_settings(None, True) == {"enabled": True, "auto": True}
    assert tr.set_settings(False, False) == {"enabled": False, "auto": False}


def test_request_queues_once_and_refuses_unready():
    it = _ready()
    a = tr.request(it["id"], "aryan", now=10)
    assert (a["status"], a["requested_by"], a["queued_at"]) == ("queued", "aryan", 10)
    assert tr.request(it["id"], "cutguy", now=20) == a
    up = es.create_iteration(it["edit_id"], "g.mp4", 1, "u")
    with pytest.raises(ValueError):
        tr.request(up["id"], "aryan")
    with pytest.raises(ValueError):
        tr.request(999, "aryan")


def test_request_keeps_done_and_retries_failed():
    it = _ready()
    tr.request(it["id"], "a", now=1)
    job = tr.claim(now=2)
    assert tr.complete(it["id"], job["token"], SEGS, "m", now=3)
    assert tr.request(it["id"], "a", now=4)["status"] == "done"
    tr._c().execute("UPDATE transcripts SET status='failed', attempts=3, error='x' WHERE iteration_id=?", (it["id"],))
    row = tr.request(it["id"], "a", now=5)
    assert (row["status"], row["attempts"], row["error"], row["queued_at"]) == ("queued", 0, None, 5)


def test_auto_only_when_on():
    it = _ready()
    tr.on_iteration_ready(it["id"])
    assert tr.get(it["id"]) is None
    tr.set_settings(None, True)
    tr.on_iteration_ready(it["id"])
    assert (tr.get(it["id"])["status"], tr.get(it["id"])["requested_by"]) == ("queued", "system")


def test_claim_oldest_and_token():
    t = _edit()
    a, b = _ready(t["id"]), _ready(t["id"])
    tr.request(b["id"], "x", now=5)
    tr.request(a["id"], "x", now=6)
    job = tr.claim(now=7)
    assert job["iteration_id"] == b["id"] and len(job["token"]) == 32
    row = tr.get(b["id"])
    assert (row["status"], row["claimed_at"], row["claim_token"]) == ("running", 7, job["token"])
    assert tr.claim(now=8)["iteration_id"] == a["id"]
    assert tr.claim(now=9) is None
    assert tr.queue_status() == {"queued": 0, "running": 2}


def test_stale_claim_requeued():
    it = _ready()
    tr.request(it["id"], "x", now=0)
    first = tr.claim(now=0)
    assert tr.claim(now=tr.CLAIM_TTL_SECONDS) is None
    again = tr.claim(now=tr.CLAIM_TTL_SECONDS + 1)
    assert again["iteration_id"] == it["id"] and again["token"] != first["token"]


def test_complete_with_stale_token_refused():
    it = _ready()
    tr.request(it["id"], "x", now=0)
    first = tr.claim(now=0)
    second = tr.claim(now=2000)
    assert not tr.complete(it["id"], first["token"], SEGS, "m")
    assert not tr.release(it["id"], first["token"], None)
    assert tr.complete(it["id"], second["token"], SEGS, "hinglish-apex-ct2", now=2100)
    row = tr.get(it["id"])
    assert (row["status"], row["segments"], row["model"], row["claim_token"]) == (
        "done", SEGS, "hinglish-apex-ct2", None)
    assert not tr.complete(it["id"], second["token"], SEGS, "m")  # not running any more


def test_release_without_error_requeues_and_errors_fail_after_three():
    it = _ready()
    tr.request(it["id"], "x", now=0)
    job = tr.claim(now=1)
    assert tr.release(it["id"], job["token"], None)
    assert (tr.get(it["id"])["status"], tr.get(it["id"])["attempts"]) == ("queued", 0)
    for n in range(1, tr.MAX_ATTEMPTS + 1):
        job = tr.claim(now=1 + n)
        assert tr.release(it["id"], job["token"], f"boom {n}")
        row = tr.get(it["id"])
        assert row["attempts"] == n and row["error"] == f"boom {n}"
    assert row["status"] == "failed"
    assert tr.claim(now=100) is None


def test_version_in_delete_grace_is_skipped_not_dropped_and_survives_undo():
    it = _ready()
    tr.request(it["id"], "x", now=0)
    es._c().execute("UPDATE iterations SET status='deleting' WHERE id=?", (it["id"],))
    assert tr.claim(now=1) is None
    assert tr.get(it["id"])["status"] == "queued"  # kept while the delete can still be undone
    es._c().execute("UPDATE iterations SET status='ready' WHERE id=?", (it["id"],))  # undo
    assert tr.claim(now=2)["iteration_id"] == it["id"]


def test_row_of_a_vanished_version_dropped_on_claim():
    it = _ready()
    tr.request(it["id"], "x", now=0)
    es._c().execute("PRAGMA foreign_keys=OFF")
    es._c().execute("DELETE FROM iterations WHERE id=?", (it["id"],))
    assert tr.claim(now=1) is None
    assert tr.get(it["id"]) is None


def test_forget_removes_rows():
    a, b = _ready(), _ready()
    tr.request(a["id"], "x")
    tr.request(b["id"], "x")
    tr.forget([a["id"]])
    assert tr.get(a["id"]) is None and tr.get(b["id"]) is not None
    tr.forget([])


@pytest.mark.asyncio
async def test_deleting_edit_files_forgets_transcripts(monkeypatch):
    async def delete_file(fid):
        pass
    monkeypatch.setattr(gdrive, "delete_file", delete_file)
    it = _ready()
    tr.request(it["id"], "x")
    deletions._grace_applied = True  # mid-process
    deletions.request_delete("edit_files", it["edit_id"], "t", username="aryan", role="admin", now=100)
    await deletions.run_due(100 + deletions.DELETE_GRACE_SECONDS)
    assert es.get_iteration(it["id"])["status"] == "deleted"
    assert tr.get(it["id"]) is None


def test_validate_segments_rejects():
    ok = tr.validate_segments([{"start": 0, "end": 0, "text": "a"}, {"start": 1, "end": 2.5, "text": ""}])
    assert ok == [{"start": 0.0, "end": 0.0, "text": "a"}, {"start": 1.0, "end": 2.5, "text": ""}]
    seg = {"start": 0, "end": 1, "text": "a"}
    for bad in ("nope", {"a": 1}, [1], [{**seg, "start": -1}], [{**seg, "start": 2}], [{**seg, "text": "x" * 2001}],
                [seg] * 20001, [{**seg, "start": "0"}], [{**seg, "start": True}], [{**seg, "end": float("nan")}],
                [{**seg, "text": 5}], [{"start": 0, "end": 1}]):
        with pytest.raises(ValueError):
            tr.validate_segments(bad)


def test_search_finds_line_with_start():
    it = _ready()
    tr.request(it["id"], "x")
    job = tr.claim()
    tr.complete(it["id"], job["token"], SEGS, "m")
    hits = tr.search("bhai")
    assert [(h["iteration_id"], h["start"], h["text"], h["edit_id"]) for h in hits] == [
        (it["id"], 1.5, "GG bhai", it["edit_id"])]
    assert tr.search("CLUTCH")[0]["start"] == 0
    assert tr.search("%") == [] and tr.search("  ") == []
