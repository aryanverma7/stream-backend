"""
The in-site notification bell: the other person's events, newest first, plus an unseen count
against a per-user high-water mark in meta. Comment and reply summaries carry the comment text,
so the bell shows a kind-based label for those instead.
"""
from core import db
from core.auth import role_for_username
from editor import editor_store as es
from editor.notifier import label

_FIELDS = "id, at, actor, kind, summary, project_id, tape_id, iteration_id"
_WHERE = ("lower(actor)<>lower(?) AND (actor<>'system' OR kind='iteration_ready')")  # recovered uploads
_LABELS = {"comment_added": "commented on", "reply_added": "replied on", "guest_notes_added": "left a guest note on"}
_STREAMER_ONLY = " AND kind<>'guest_notes_added'"  # unapproved guest notes never reach the editor


def _seen_key(username: str) -> str:
    return f"bell_seen_{username.lower()}"


def _seen(username: str) -> int:
    return int(db.meta_get(_seen_key(username)) or 0)


def _where(username: str) -> str:
    return _WHERE + ("" if role_for_username(username) == "admin" else _STREAMER_ONLY)


def feed(username: str, after_id: "int | None" = None, limit: int = 50) -> dict:
    conn = es._c()
    where = _where(username)
    items = [dict(r) for r in conn.execute(
        f"SELECT {_FIELDS} FROM events WHERE {where} AND id>? ORDER BY id DESC LIMIT ?",
        (username, after_id or 0, limit))]
    for i in items:
        if i["kind"] in _LABELS:
            who = i["actor"].removeprefix("guest:")
            i["summary"] = f"{who} {_LABELS[i['kind']]} {label(i['tape_id'], i['iteration_id'])}"
    unseen = conn.execute(f"SELECT COUNT(*) FROM events WHERE {where} AND id>?",
                          (username, _seen(username))).fetchone()[0]
    return {"items": items, "unseen": unseen}


def mark_seen(username: str, up_to_id: int) -> None:
    """Clamped to the newest event: a marker past it would hide every event still to come."""
    newest = es._c().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    db.meta_set(_seen_key(username), str(max(_seen(username), min(up_to_id, newest))))
