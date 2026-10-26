import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import db
import editor_bell
import editor_store as es


@pytest.fixture(autouse=True)
def setup(tmp_path):
    db.use_path_for_tests(tmp_path / "b.db")


def _world():
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    t = es.create_tape(p["id"], "Jett dash", now=1.0)
    it = es.create_iteration(t["id"], "a.mp4", 1, "u", now=1.0)
    return p, t, it


def test_bell_lists_only_other_persons_events():
    p, t, it = _world()
    ids = dict(p=p["id"], t=t["id"], i=it["id"])
    mine = es.record_event("Aryan", "tape_created", "Aryan added tape", project_id=ids["p"], now=2.0)
    sys_ev = es.record_event("system", "delete_done", "Deleted x", now=3.0)
    c = es.record_event("ed", "comment_added", "ed commented: secret snippet", project_id=ids["p"],
                        tape_id=ids["t"], iteration_id=ids["i"], now=4.0)
    r = es.record_event("ed", "tape_created", "ed added tape Jett dash", project_id=ids["p"],
                        tape_id=ids["t"], now=5.0)
    feed = editor_bell.feed("aryan")  # case-insensitive self match
    assert [i["id"] for i in feed["items"]] == [r, c]
    assert feed["items"][0] == {"id": r, "at": 5.0, "actor": "ed", "kind": "tape_created",
                                "summary": "ed added tape Jett dash", "project_id": ids["p"],
                                "tape_id": ids["t"], "iteration_id": None}
    assert "secret" not in feed["items"][1]["summary"]
    assert feed["items"][1]["summary"] == "ed commented on Jett dash v1"
    assert [i["id"] for i in editor_bell.feed("ed")["items"]] == [mine]
    assert sys_ev not in [i["id"] for i in editor_bell.feed("ed")["items"]]
    assert [i["id"] for i in editor_bell.feed("aryan", after_id=c)["items"]] == [r]
    assert [i["id"] for i in editor_bell.feed("aryan", limit=1)["items"]] == [r]


def test_bell_unseen_and_mark_seen():
    a = es.record_event("ed", "tape_created", "x", now=1.0)
    b = es.record_event("ed", "tape_created", "y", now=2.0)
    assert editor_bell.feed("Aryan")["unseen"] == 2
    editor_bell.mark_seen("Aryan", a)
    assert editor_bell.feed("aryan")["unseen"] == 1
    editor_bell.mark_seen("aryan", b)
    editor_bell.mark_seen("aryan", a)  # never moves back
    assert editor_bell.feed("aryan")["unseen"] == 0
    assert db.meta_get("bell_seen_aryan") == str(b)
    assert editor_bell.feed("ed")["unseen"] == 0
