import sqlite3

import pytest

from core import db
from editor import editor_store as es


@pytest.fixture(autouse=True)
def _db(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")


def _proj(now=100.0):
    return es.create_project("Ranked", "2026-10-20", "Late night", "vod.mp4", 5000, now=now)


def test_project_lifecycle_and_rename():
    p = _proj()
    assert p["vod_status"] == "uploading" and p["stream_title"] == "Late night"
    es.set_vod_upload(p["id"], "https://up/1")
    es.set_project_folder(p["id"], "fold")
    r = es.rename_project(p["id"], "Renamed", now=200.0)
    assert r["name"] == "Renamed" and r["updated_at"] == 200.0 and r["drive_folder_id"] == "fold"
    assert es.mark_vod_ready(p["id"], "fid", now=300.0)["vod_status"] == "ready"
    es.create_tape(p["id"], "t1", now=1.0)
    assert es.list_projects()[0]["tape_count"] == 1
    es.create_project("Newer", "2026-10-21", "", "v.mp4", None, now=400.0)
    assert [x["name"] for x in es.list_projects()] == ["Newer", "Renamed"]
    assert es.get_project(999) is None and es.rename_project(999, "x") is None


def test_download_click_keeps_first_time():
    p = _proj()
    assert es.record_download_click(p["id"], now=500.0)["download_clicked_at"] == 500.0
    assert es.record_download_click(p["id"], now=900.0)["download_clicked_at"] == 500.0


def test_tape_for_missing_project_rejected():
    with pytest.raises(sqlite3.IntegrityError):
        es.create_tape(999, "t")


def test_versions_increase_per_tape():
    p = _proj()
    t1, t2 = es.create_tape(p["id"], "a"), es.create_tape(p["id"], "b")
    assert es.next_version(t1["id"]) == 1
    i1 = es.create_iteration(t1["id"], "a.mp4", 10, "u")
    i2 = es.create_iteration(t1["id"], "a2.mp4", 10, "u")
    assert (i1["version"], i2["version"]) == (1, 2)
    assert es.create_iteration(t2["id"], "b.mp4", 10, "u")["version"] == 1
    assert [i["version"] for i in es.list_iterations(t1["id"])] == [1, 2]


def test_complete_iteration_is_idempotent():
    t = es.create_tape(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 10, "u")
    row, changed = es.complete_iteration(i["id"], "F1", now=5.0)
    assert changed and row["status"] == "ready" and row["drive_file_id"] == "F1" and row["uploaded_at"] == 5.0
    row, changed = es.complete_iteration(i["id"], "F1", now=9.0)
    assert not changed and row["uploaded_at"] == 5.0


def test_complete_moves_tape_to_in_review():
    p = _proj()
    for start in ("cutting", "changes_asked"):
        t = es.create_tape(p["id"], start)
        es.update_tape(t["id"], status=start)
        es.complete_iteration(es.create_iteration(t["id"], "a.mp4", 1, "u")["id"], "F")
        assert es.get_tape(t["id"])["status"] == "in_review"
    t = es.create_tape(p["id"], "fin")
    es.update_tape(t["id"], status="final")
    es.complete_iteration(es.create_iteration(t["id"], "a.mp4", 1, "u")["id"], "F")
    assert es.get_tape(t["id"])["status"] == "final"


def test_complete_rejects_dead_statuses():
    t = es.create_tape(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    for dead in ("deleting", "deleted"):
        db.get().execute("UPDATE iterations SET status=? WHERE id=?", (dead, i["id"]))
        with pytest.raises(ValueError):
            es.complete_iteration(i["id"], "F")


def test_abandoned_iteration_found_later_becomes_ready():
    t = es.create_tape(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u", now=1.0)
    assert es.abandon_iteration(i["id"], now=2.0) is True
    row, changed = es.complete_iteration(i["id"], "F", now=3.0)
    assert changed and row["status"] == "ready" and row["drive_file_id"] == "F"


def test_unknown_status_rejected():
    t = es.create_tape(_proj()["id"], "a")
    with pytest.raises(ValueError):
        es.update_tape(t["id"], status="bogus")
    assert es.update_tape(t["id"], notes="n", now=7.0)["notes"] == "n"
    assert es.update_tape(999, notes="n") is None


def test_thumbnail_over_limit_rejected():
    t = es.create_tape(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    with pytest.raises(ValueError):
        es.set_iteration_media(i["id"], thumbnail=b"x" * 262145)
    with pytest.raises(ValueError):
        es.set_iteration_media(i["id"], checks_json="x" * 16385)
    es.set_iteration_media(i["id"], checks_json="{}", thumbnail=b"x" * 262144)
    assert len(es.get_iteration(i["id"])["thumbnail"]) == 262144
    assert "thumbnail" not in es.list_iterations(t["id"])[0]


def test_previous_ready_iteration():
    t = es.create_tape(_proj()["id"], "a")
    ids = [es.create_iteration(t["id"], f"{n}.mp4", 1, "u")["id"] for n in range(3)]
    es.complete_iteration(ids[0], "F0")
    assert es.previous_ready_iteration(t["id"], 3)["id"] == ids[0]
    assert es.previous_ready_iteration(t["id"], 1) is None


def test_events_since_excludes_actor():
    a = es.record_event("ann", "comment", "hi", now=1.0)
    b = es.record_event("bob", "comment", "yo", project_id=None, now=2.0)
    assert [e["id"] for e in es.events_since(0)] == [a, b]
    assert [e["id"] for e in es.events_since(0, exclude_actor="ann")] == [b]
    assert es.events_since(b) == []


def test_complete_iteration_is_atomic():
    t = es.create_tape(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    db.get().execute("CREATE TRIGGER boom BEFORE UPDATE ON tapes BEGIN SELECT RAISE(ABORT, 'x'); END")
    with pytest.raises(sqlite3.Error):  # the second UPDATE fails
        es.complete_iteration(i["id"], "F")
    assert es.get_iteration(i["id"])["status"] == "uploading"


def test_download_click_bumps_updated_at():
    p = _proj()
    assert es.record_download_click(p["id"], now=500.0)["updated_at"] == 500.0


# -- clips and reels --

def _clip(name="Ace", duration_s=42.0, now=100.0):
    return es.create_project(name, "2026-10-20", "", f"{name}.mp4", 10, now=now, duration_s=duration_s)


def test_short_upload_becomes_a_clip_with_one_reel():
    c = _clip(duration_s=299)
    assert c["kind"] == "clip" and c["duration_s"] == 299
    [reel] = es.list_tapes(c["id"])
    assert (reel["kind"], reel["name"], reel["source_project_ids"]) == ("reel", "Ace", [c["id"]])
    assert c["reel_tape_id"] == reel["id"] and es.reel_for_clip(c["id"])["id"] == reel["id"]
    assert es.get_tape(reel["id"])["source_project_ids"] == [c["id"]]
    for d in (300, None):
        v = _clip(f"Vod{d}", duration_s=d)
        assert v["kind"] == "vod" and v["reel_tape_id"] is None and es.list_tapes(v["id"]) == []
        assert es.reel_for_clip(v["id"]) is None
    assert es.CLIP_MAX_SECONDS == 300


def test_second_reel_on_a_clip_is_refused():
    c = _clip()
    with pytest.raises(ValueError, match="A clip has exactly one reel"):
        es.create_tape(c["id"], "another")
    assert len(es.list_tapes(c["id"])) == 1
    t = es.create_tape(_proj()["id"], "ordinary")
    assert t["kind"] == "tape" and t["source_project_ids"] == []


def test_a_clip_cannot_be_in_two_reels():
    a, b = _clip("A"), _clip("B")
    with pytest.raises(sqlite3.IntegrityError):
        db.get().execute("INSERT INTO reel_sources(tape_id, project_id) VALUES(?,?)", (b["reel_tape_id"], a["id"]))
