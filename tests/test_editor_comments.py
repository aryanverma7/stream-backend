import pytest

from core import db
from editor import editor_comments as ec
from editor import editor_store as es


@pytest.fixture(autouse=True)
def _db(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")


def _tape():
    p = es.create_project("P", "2026-10-20", "", "v.mp4", 1, now=1.0)
    return es.create_tape(p["id"], "t", now=1.0)


def _ready(tape_id):
    it = es.create_iteration(tape_id, "f.mp4", 1, "u", now=1.0)
    return es.complete_iteration(it["id"], "fid", now=2.0)[0]


def _two():
    t = _tape()
    return t, _ready(t["id"]), _ready(t["id"])


def test_general_point_and_range_comments_validate():
    it = _ready(_tape()["id"])
    g = ec.add_comment(it["id"], "a", "fix", "general", now=1.0)
    assert g["start_s"] is None and g["end_s"] is None
    pt = ec.add_comment(it["id"], "a", "keep", "point", start_s=5, now=2.0)
    assert pt["start_s"] == 5 and pt["end_s"] is None
    rg = ec.add_comment(it["id"], "a", "question", "range", start_s=5, end_s=9, now=3.0)
    assert rg["end_s"] == 9
    for kw in (dict(kind="nope", body="x"), dict(kind="fix", body="  "), dict(kind="fix", body="x", start_s=-1),
               dict(kind="fix", body="x", end_s=4), dict(kind="fix", body="x", start_s=5, end_s=5),
               dict(kind="fix", body="x", start_s=5, end_s=2)):
        with pytest.raises(ValueError):
            ec.add_comment(it["id"], "a", **kw)
    assert len(ec.comments_for_iteration(it["id"])) == 3


def test_only_author_can_edit():
    it = _ready(_tape()["id"])
    c = ec.add_comment(it["id"], "a", "fix", "old", now=1.0)
    with pytest.raises(PermissionError):
        ec.edit_comment(c["id"], "b", body="hacked")
    e = ec.edit_comment(c["id"], "a", body="new", kind="keep", now=5.0)
    assert e["body"] == "new" and e["kind"] == "keep" and e["updated_at"] == 5.0


def test_reply_thread_order():
    it = _ready(_tape()["id"])
    c = ec.add_comment(it["id"], "a", "fix", "q", now=1.0)
    ec.add_reply(c["id"], "b", "first", now=2.0)
    ec.add_reply(c["id"], "a", "second", now=3.0)
    assert [r["body"] for r in ec.comments_for_iteration(it["id"])[0]["replies"]] == ["first", "second"]


def test_carry_over_takes_unresolved_only():
    _, v1, v2 = _two()
    a = ec.add_comment(v1["id"], "a", "fix", "A", start_s=222, now=1.0)
    b = ec.add_comment(v1["id"], "a", "fix", "B", now=1.0)
    ec.set_resolved(b["id"], v1["id"], True, "a", now=2.0)
    assert ec.carry_over(v2["id"]) == 1
    got = ec.comments_for_iteration(v2["id"])
    assert [(c["id"], c["carried"], c["resolved"]) for c in got] == [(a["id"], True, False)]
    assert got[0]["origin_version"] == 1 and got[0]["start_s"] == 222
    assert len(ec.comments_for_iteration(v1["id"])) == 2


def test_carry_over_is_idempotent():
    _, v1, v2 = _two()
    ec.add_comment(v1["id"], "a", "fix", "A", now=1.0)
    assert ec.carry_over(v2["id"]) == 1
    assert ec.carry_over(v2["id"]) == 0
    assert len(ec.comments_for_iteration(v2["id"])) == 1


def test_resolving_on_v2_does_not_change_v1():
    _, v1, v2 = _two()
    a = ec.add_comment(v1["id"], "a", "fix", "A", now=1.0)
    ec.carry_over(v2["id"])
    ec.set_resolved(a["id"], v2["id"], True, "a", now=3.0)
    assert ec.comments_for_iteration(v2["id"])[0]["resolved"]
    assert not ec.comments_for_iteration(v1["id"])[0]["resolved"]
    ec.set_resolved(a["id"], v2["id"], False, "a", now=4.0)
    assert not ec.comments_for_iteration(v2["id"])[0]["resolved"]


def test_carry_chain_v1_to_v3():
    t, v1, v2 = _two()
    v3 = _ready(t["id"])
    a = ec.add_comment(v1["id"], "a", "fix", "A", now=1.0)
    ec.carry_over(v2["id"])
    assert ec.carry_over(v3["id"]) == 1
    got = ec.comments_for_iteration(v3["id"])
    assert got[0]["id"] == a["id"] and got[0]["origin_version"] == 1 and got[0]["carried"]


def test_search_finds_reply_text_and_escapes_wildcards():
    t, v1, _ = _two()
    c = ec.add_comment(v1["id"], "a", "fix", "Trim the INTRO", start_s=7, now=1.0)
    ec.add_comment(v1["id"], "a", "fix", "100% sure", now=2.0)
    ec.add_comment(v1["id"], "a", "fix", "a_b", now=2.5)
    ec.add_reply(c["id"], "b", "music too loud", now=3.0)
    hits = ec.search("intro")
    assert [(h["kind"], h["comment_id"], h["version"], h["tape_id"], h["start_s"]) for h in hits] == \
        [("comment", c["id"], 1, t["id"], 7)]
    assert "project_id" in hits[0] and "INTRO" in hits[0]["snippet"]
    r = ec.search("LOUD")
    assert r[0]["kind"] == "reply" and r[0]["comment_id"] == c["id"]
    assert len(ec.search("%")) == 1 and len(ec.search("_")) == 1 and ec.search("\\") == []
    assert ec.search("  ") == [] and ec.search("") == []
    assert len(ec.search("a", limit=1)) == 1


def test_start_over_resolves_all_open_comments_on_that_version_only():
    t = _tape()
    v1 = _ready(t["id"])
    a = ec.add_comment(v1["id"], "cutguy", "fix", "one", now=3.0)
    b = ec.add_comment(v1["id"], "cutguy", "fix", "two", now=3.0)
    ec.set_resolved(b["id"], v1["id"], True, "aryan", now=4.0)
    v2 = _ready(t["id"])
    ec.carry_over(v2["id"])
    c = ec.add_comment(v2["id"], "cutguy", "question", "three", now=5.0)
    assert ec.start_over(v2["id"], "aryan", now=6.0) == 2
    on_v2 = {r["id"]: r for r in ec.comments_for_iteration(v2["id"])}
    assert set(on_v2) == {a["id"], c["id"]} and all(r["resolved"] for r in on_v2.values())
    assert on_v2[c["id"]]["resolved_by"] == "aryan" and on_v2[c["id"]]["resolved_at"] == 6.0
    on_v1 = {r["id"]: r["resolved"] for r in ec.comments_for_iteration(v1["id"])}
    assert on_v1 == {a["id"]: False, b["id"]: True}
    [ev] = [e for e in es.events_since(0) if e["kind"] == "start_over"]
    assert (ev["actor"], ev["iteration_id"], ev["tape_id"]) == ("aryan", v2["id"], t["id"])
    assert ec.start_over(v2["id"], "aryan", now=7.0) == 0
    with pytest.raises(ValueError):
        ec.start_over(999, "aryan")


def test_add_comment_with_same_client_id_returns_existing_and_one_event():
    it = _ready(_tape()["id"])
    a = ec.add_comment(it["id"], "a", "fix", "x", client_id="abcdefgh1", now=1.0)
    b = ec.add_comment(it["id"], "a", "fix", "x", client_id="abcdefgh1", now=2.0)
    assert a["id"] == b["id"]
    assert [e["kind"] for e in es.events_since(0)].count("comment_added") == 1


def test_add_reply_same_client_id_dedupes():
    it = _ready(_tape()["id"])
    c = ec.add_comment(it["id"], "a", "fix", "x", now=1.0)
    a = ec.add_reply(c["id"], "b", "hi", client_id="abcdefgh1", now=1.0)
    b = ec.add_reply(c["id"], "b", "hi", client_id="abcdefgh1", now=2.0)
    assert a["id"] == b["id"]
    assert [e["kind"] for e in es.events_since(0)].count("reply_added") == 1


def test_client_id_columns_added_to_old_db():
    db.ensure("""CREATE TABLE comments(id INTEGER PRIMARY KEY, iteration_id INTEGER, author TEXT, kind TEXT,
                 start_s REAL, end_s REAL, body TEXT, created_at REAL, updated_at REAL);
                 CREATE TABLE replies(id INTEGER PRIMARY KEY, comment_id INTEGER, author TEXT, body TEXT,
                 created_at REAL);""")
    conn = ec._cc()
    for t in ("comments", "replies"):
        assert "client_id" in {r["name"] for r in conn.execute(f"PRAGMA table_info({t})")}


def test_start_over_uses_the_latest_ready_version():
    t = _tape()
    v1 = _ready(t["id"])
    ec.add_comment(v1["id"], "cutguy", "fix", "one", now=3.0)
    up = es.create_iteration(t["id"], "f.mp4", 1, "u", now=4.0)  # v2 still uploading
    assert ec.start_over(v1["id"], "aryan", now=5.0) == 1
    with pytest.raises(ValueError, match="latest version"):
        ec.start_over(up["id"], "aryan", now=6.0)


def test_a_client_id_is_scoped_to_its_author_and_target():
    t = _tape()
    v1, v2 = _ready(t["id"]), _ready(t["id"])
    c = ec.add_comment(v1["id"], "cutguy", "fix", "x", client_id="cid-00001")
    assert ec.add_comment(v1["id"], "cutguy", "fix", "x", client_id="cid-00001")["id"] == c["id"]
    for author, it in (("aryan", v1), ("cutguy", v2)):
        with pytest.raises(ec.ClientIdTaken):
            ec.add_comment(it["id"], author, "fix", "x", client_id="cid-00001")
    assert ec.by_client_id("comments", "cid-00001", "aryan", v1["id"]) is None
    other = ec.add_comment(v1["id"], "aryan", "fix", "y")
    r = ec.add_reply(c["id"], "aryan", "ok", client_id="rid-00001")
    assert ec.add_reply(c["id"], "aryan", "ok", client_id="rid-00001")["id"] == r["id"]
    for author, cid in (("cutguy", c["id"]), ("aryan", other["id"])):
        with pytest.raises(ec.ClientIdTaken):
            ec.add_reply(cid, author, "ok", client_id="rid-00001")


def test_search_skips_soft_deleted_projects():
    t = _tape()
    it = _ready(t["id"])
    ec.add_comment(it["id"], "a", "fix", "needle here", now=3.0)
    assert len(ec.search("needle")) == 1
    es._set("projects", t["project_id"], None, deleted_at=9.0)
    assert ec.search("needle") == []
