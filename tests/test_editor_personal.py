import pytest

import db
import editor_personal as ep
import editor_store as es


@pytest.fixture(autouse=True)
def _db(tmp_path):
    db.use_path_for_tests(tmp_path / "x.db")


def test_todos_are_private_per_owner():
    a = ep.add_todo("aryan", "export thumbnails", now=1.0)
    ep.add_todo("cutguy", "colour pass", now=2.0)
    assert [t["text"] for t in ep.list_todos("aryan")] == ["export thumbnails"]
    assert ep.update_todo("cutguy", a["id"], done=True) is None
    assert ep.delete_todo("cutguy", a["id"]) is False
    assert ep.list_todos("aryan")[0]["done"] is False
    done = ep.update_todo("aryan", a["id"], done=True, now=3.0)
    assert done["done"] is True and done["text"] == "export thumbnails" and done["updated_at"] == 3.0
    assert ep.delete_todo("aryan", a["id"]) is True
    assert ep.list_todos("aryan") == []
    with pytest.raises(ValueError):
        ep.add_todo("aryan", "   ")


def test_tape_has_new_ignores_own_events():
    p = es.create_project("Ace", "2026-10-20", "", "v.mp4", 5, now=1.0)
    t = es.create_tape(p["id"], "Cut", now=1.0)
    assert ep.tape_has_new("cutguy", t["id"]) is False  # nothing happened yet
    es.record_event("cutguy", "comment_added", "mine", tape_id=t["id"], now=10.0)
    assert ep.tape_has_new("cutguy", t["id"]) is False  # own event
    assert ep.tape_has_new("aryan", t["id"]) is True  # never seen + someone else's event
    ep.mark_tape_seen("aryan", t["id"], now=11.0)
    assert ep.tape_has_new("aryan", t["id"]) is False
    es.record_event("aryan", "comment_added", "his", tape_id=t["id"], now=12.0)
    assert ep.tape_has_new("aryan", t["id"]) is False
    es.record_event("cutguy", "reply_added", "new", tape_id=t["id"], now=13.0)
    assert ep.tape_has_new("aryan", t["id"]) is True


def test_position_roundtrip():
    assert ep.get_position("aryan", 7) == 0.0
    ep.set_position("aryan", 7, 42.5, now=1.0)
    ep.set_position("aryan", 7, 60.0, now=2.0)
    assert ep.get_position("aryan", 7) == 60.0
    assert ep.get_position("cutguy", 7) == 0.0
