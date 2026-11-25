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
    es.create_edit(p["id"], "t1", now=1.0)
    assert es.list_projects()[0]["edit_count"] == 1
    es.create_project("Newer", "2026-10-21", "", "v.mp4", None, now=400.0)
    assert [x["name"] for x in es.list_projects()] == ["Newer", "Renamed"]
    assert es.get_project(999) is None and es.rename_project(999, "x") is None


def test_download_click_keeps_first_time():
    p = _proj()
    assert es.record_download_click(p["id"], now=500.0)["download_clicked_at"] == 500.0
    assert es.record_download_click(p["id"], now=900.0)["download_clicked_at"] == 500.0


def test_edit_for_missing_project_rejected():
    with pytest.raises(sqlite3.IntegrityError):
        es.create_edit(999, "t")


def test_versions_increase_per_edit():
    p = _proj()
    t1, t2 = es.create_edit(p["id"], "a"), es.create_edit(p["id"], "b")
    assert es.next_version(t1["id"]) == 1
    i1 = es.create_iteration(t1["id"], "a.mp4", 10, "u")
    i2 = es.create_iteration(t1["id"], "a2.mp4", 10, "u")
    assert (i1["version"], i2["version"]) == (1, 2)
    assert es.create_iteration(t2["id"], "b.mp4", 10, "u")["version"] == 1
    assert [i["version"] for i in es.list_iterations(t1["id"])] == [1, 2]


def test_complete_iteration_is_idempotent():
    t = es.create_edit(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 10, "u")
    row, changed = es.complete_iteration(i["id"], "F1", now=5.0)
    assert changed and row["status"] == "ready" and row["drive_file_id"] == "F1" and row["uploaded_at"] == 5.0
    row, changed = es.complete_iteration(i["id"], "F1", now=9.0)
    assert not changed and row["uploaded_at"] == 5.0


def test_complete_moves_edit_to_in_review():
    p = _proj()
    for start in ("cutting", "changes_asked"):
        t = es.create_edit(p["id"], start)
        es.update_edit(t["id"], status=start)
        es.complete_iteration(es.create_iteration(t["id"], "a.mp4", 1, "u")["id"], "F")
        assert es.get_edit(t["id"])["status"] == "in_review"
    t = es.create_edit(p["id"], "fin")
    es.update_edit(t["id"], status="final")
    es.complete_iteration(es.create_iteration(t["id"], "a.mp4", 1, "u")["id"], "F")
    assert es.get_edit(t["id"])["status"] == "final"


def test_complete_rejects_dead_statuses():
    t = es.create_edit(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    for dead in ("deleting", "deleted"):
        db.get().execute("UPDATE iterations SET status=? WHERE id=?", (dead, i["id"]))
        with pytest.raises(ValueError):
            es.complete_iteration(i["id"], "F")


def test_abandoned_iteration_found_later_becomes_ready():
    t = es.create_edit(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u", now=1.0)
    assert es.abandon_iteration(i["id"], now=2.0) is True
    row, changed = es.complete_iteration(i["id"], "F", now=3.0)
    assert changed and row["status"] == "ready" and row["drive_file_id"] == "F"


def test_unknown_status_rejected():
    t = es.create_edit(_proj()["id"], "a")
    with pytest.raises(ValueError):
        es.update_edit(t["id"], status="bogus")
    assert es.update_edit(t["id"], notes="n", now=7.0)["notes"] == "n"
    assert es.update_edit(999, notes="n") is None


def test_thumbnail_over_limit_rejected():
    t = es.create_edit(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    with pytest.raises(ValueError):
        es.set_iteration_media(i["id"], thumbnail=b"x" * 262145)
    with pytest.raises(ValueError):
        es.set_iteration_media(i["id"], checks_json="x" * 16385)
    es.set_iteration_media(i["id"], checks_json="{}", thumbnail=b"x" * 262144)
    assert len(es.get_iteration(i["id"])["thumbnail"]) == 262144
    assert "thumbnail" not in es.list_iterations(t["id"])[0]


def test_previous_ready_iteration():
    t = es.create_edit(_proj()["id"], "a")
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
    t = es.create_edit(_proj()["id"], "a")
    i = es.create_iteration(t["id"], "a.mp4", 1, "u")
    db.get().execute("CREATE TRIGGER boom BEFORE UPDATE ON edits BEGIN SELECT RAISE(ABORT, 'x'); END")
    with pytest.raises(sqlite3.Error):  # the second UPDATE fails
        es.complete_iteration(i["id"], "F")
    assert es.get_iteration(i["id"])["status"] == "uploading"


def test_download_click_bumps_updated_at():
    p = _proj()
    assert es.record_download_click(p["id"], now=500.0)["updated_at"] == 500.0


# -- tapes and reels --

def _tape(name="Ace", duration_s=42.0, now=100.0):
    return es.create_project(name, "2026-10-20", "", f"{name}.mp4", 10, now=now, duration_s=duration_s)


def test_short_upload_becomes_a_tape_with_one_reel():
    c = _tape(duration_s=299)
    assert c["kind"] == "tape" and c["duration_s"] == 299
    [reel] = es.list_edits(c["id"])
    assert (reel["kind"], reel["name"], reel["source_project_ids"]) == ("reel", "Ace", [c["id"]])
    assert c["reel_edit_id"] == reel["id"] and es.reel_for_tape(c["id"])["id"] == reel["id"]
    assert es.get_edit(reel["id"])["source_project_ids"] == [c["id"]]
    for d in (300, None):
        v = _tape(f"Vod{d}", duration_s=d)
        assert v["kind"] == "vod" and v["reel_edit_id"] is None and es.list_edits(v["id"]) == []
        assert es.reel_for_tape(v["id"]) is None
    assert es.TAPE_MAX_SECONDS == 300


def test_second_reel_on_a_tape_is_refused():
    c = _tape()
    with pytest.raises(ValueError, match="A tape has exactly one reel"):
        es.create_edit(c["id"], "another")
    assert len(es.list_edits(c["id"])) == 1
    t = es.create_edit(_proj()["id"], "ordinary")
    assert t["kind"] is None and t["source_project_ids"] == []  # its first version decides


def test_a_tape_cannot_be_in_two_reels():
    a, b = _tape("A"), _tape("B")
    with pytest.raises(sqlite3.IntegrityError):
        db.get().execute("INSERT INTO reel_sources(edit_id, project_id) VALUES(?,?)", (b["reel_edit_id"], a["id"]))


def test_create_project_writes_its_meta_key_in_the_same_transaction(monkeypatch):
    p = es.create_project("A", "2026-10-20", "", "v.mp4", 5, meta_key="uploader_job_x")
    assert db.meta_get("uploader_job_x") == str(p["id"])

    def crash(*a):
        raise RuntimeError("power cut")
    monkeypatch.setattr(db, "meta_set", crash)
    with pytest.raises(RuntimeError):
        es.create_project("B", "2026-10-20", "", "v.mp4", 5, meta_key="uploader_job_y")
    assert [x["name"] for x in es.list_projects()] == ["A"]


# -- the 2026-11-25 rename: an app.db from before it is migrated in place --

_OLD_SCHEMA = """
CREATE TABLE projects(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, stream_date TEXT NOT NULL,
  stream_title TEXT NOT NULL DEFAULT '', vod_filename TEXT NOT NULL, vod_size INTEGER,
  vod_file_id TEXT,
  vod_status TEXT NOT NULL CHECK (vod_status IN ('uploading','ready','deleting','deleted')),
  vod_upload_url TEXT, drive_folder_id TEXT, download_clicked_at REAL, deleted_at REAL,
  kind TEXT NOT NULL DEFAULT 'vod' CHECK (kind IN ('vod','clip')), duration_s REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE tapes(
  id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'tape' CHECK (kind IN ('tape','reel')),
  status TEXT NOT NULL DEFAULT 'cutting' CHECK (status IN ('cutting','in_review','changes_asked','final')),
  notes TEXT NOT NULL DEFAULT '', drive_folder_id TEXT, files_deleted_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE iterations(
  id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL REFERENCES tapes(id),
  version INTEGER NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleting','deleted','abandoned')),
  drive_file_id TEXT, upload_url TEXT, checks_json TEXT, thumbnail BLOB, uploaded_at REAL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (tape_id, version));
CREATE TABLE reel_sources(
  tape_id INTEGER NOT NULL REFERENCES tapes(id),
  project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id), PRIMARY KEY (tape_id, project_id));
CREATE TABLE events(
  id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  project_id INTEGER, tape_id INTEGER, iteration_id INTEGER, comment_id INTEGER, summary TEXT NOT NULL);
CREATE TABLE tape_seen(username TEXT, tape_id INTEGER, seen_at REAL, PRIMARY KEY (username, tape_id));
CREATE TABLE attachments(
  id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL REFERENCES tapes(id),
  comment_id INTEGER, reply_id INTEGER, author TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
  size INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleted')),
  drive_file_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE delete_requests(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('vod','tape_files','project')),
  target_id INTEGER NOT NULL, label TEXT NOT NULL, requested_by TEXT NOT NULL,
  requested_at REAL NOT NULL, delete_at REAL NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','done','cancelled')));
INSERT INTO projects VALUES(1,'Ranked','2026-10-20','','vod.mp4',5,NULL,'ready',NULL,NULL,NULL,NULL,'vod',NULL,1,1);
INSERT INTO projects VALUES(2,'Ace','2026-10-21','','ace.mp4',5,NULL,'ready',NULL,NULL,NULL,NULL,'clip',42,1,1);
INSERT INTO tapes VALUES(10,1,'Highlights','tape','final','',NULL,NULL,1,1);
INSERT INTO tapes VALUES(11,2,'Ace','reel','cutting','',NULL,NULL,1,1);
INSERT INTO tapes VALUES(12,1,'Not started','tape','cutting','',NULL,NULL,1,1);
INSERT INTO reel_sources VALUES(11,2);
INSERT INTO iterations VALUES(20,10,1,'a.mp4',9,'ready','f1',NULL,NULL,NULL,50,1,1);
INSERT INTO events VALUES(1,1,'aryan','tape_created',1,10,NULL,NULL,'aryan added tape Highlights');
INSERT INTO tape_seen VALUES('cutguy',10,5);
INSERT INTO attachments VALUES(30,10,NULL,NULL,'aryan','a.png','image/png',3,'ready','f2',1,1);
INSERT INTO delete_requests VALUES(40,'tape_files',10,'Highlights','aryan',1,2,'pending');
"""


def test_migration_stops_on_an_unexpected_check(tmp_path):
    path = tmp_path / "odd.db"
    old = sqlite3.connect(path)
    old.executescript(_OLD_SCHEMA.replace("CHECK (kind IN ('tape','reel'))", "CHECK (kind IN ('tape','reel','x'))"))
    old.close()
    db.use_path_for_tests(path)
    with pytest.raises(RuntimeError, match="not found in table edits"):
        es._c()
    assert db.get().execute("SELECT COUNT(*) FROM tapes").fetchone()[0] == 3  # rolled back whole


def test_short_limit_allows_for_the_site_rounding_to_a_tenth(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")
    p = es.create_project("Ranked", "2026-10-20", "", "v.mp4", 5)
    assert es.detect_kind(es.create_edit(p["id"], "a"), 1080, 1920, 180.04) == "clip"
    assert es.detect_kind(es.create_edit(p["id"], "b"), 1920, 1080, 180.05) == "video"
    assert es.detect_kind(es.create_edit(p["id"], "c"), 1920, 1080, 180.0) == "video"
    assert es.detect_kind(es.create_edit(p["id"], "d"), 1080, 1920, 180.0) == "clip"
    with pytest.raises(ValueError):
        es.detect_kind(es.create_edit(p["id"], "e"), 1920, 1080, 179.9)


def test_old_schema_is_migrated_in_place_and_keeps_its_data(tmp_path):
    from editor import deletions, editor_personal as ep
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(_OLD_SCHEMA)
    old.close()
    db.use_path_for_tests(path)
    deletions._c()
    ep._c()
    es._migrate()  # a second run is a no-op
    assert es.get_project(1)["kind"] == "vod" and es.get_project(2)["kind"] == "tape"
    assert es.get_project(2)["reel_edit_id"] == 11
    video, reel = es.get_edit(10), es.get_edit(11)
    assert video["kind"] is None and video["status"] == "final" and video["final_upload_at"] == 50
    assert reel["kind"] == "reel" and reel["source_project_ids"] == [2]
    assert es.list_iterations(10)[0]["id"] == 20
    [ev] = es.events_since(0)
    assert ev["kind"] == "edit_created" and ev["edit_id"] == 10
    conn = db.get()
    assert conn.execute("SELECT edit_id FROM edit_seen").fetchone()[0] == 10
    assert conn.execute("SELECT edit_id FROM attachments").fetchone()[0] == 10
    assert conn.execute("SELECT kind FROM delete_requests").fetchone()[0] == "edit_files"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not names & {"tapes", "tape_seen", "edits_new", "projects_new"}
    assert "edits" in conn.execute("SELECT sql FROM sqlite_master WHERE name='iterations'").fetchone()[0]
    short = es.create_edit(1, "Short")
    assert short["kind"] is None and es.get_edit(12)["kind"] is None  # no version yet: no kind
    assert es.detect_kind(short, 1080, 1920, 30) == "clip" and es.get_edit(short["id"])["kind"] == "clip"
    # an old output with versions has no kind either: a vertical short next version makes it a clip
    assert es.detect_kind(es.get_edit(10), 1080, 1920, 30) == "clip" and es.get_edit(10)["kind"] == "clip"
    with pytest.raises(sqlite3.IntegrityError):  # foreign keys still enforced against edits
        conn.execute("INSERT INTO iterations(edit_id, version, filename, size, status, created_at, updated_at)"
                     " VALUES(999,1,'x',1,'uploading',1,1)")
