"""Editor app comments: point/range/general comments, replies, per-iteration resolved state, carry-over, search.

A comment row is shared across versions; only comment_state (resolved or not) is per iteration.
"""
import time

import db
import editor_store as es
from editor_store import _all, _c, _one

KINDS = ("fix", "keep", "question")
SNIPPET_CHARS = 120

_DDL = """
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY, iteration_id INTEGER NOT NULL REFERENCES iterations(id),
  author TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('fix','keep','question')),
  start_s REAL, end_s REAL, body TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS replies(
  id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL REFERENCES comments(id),
  author TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS comment_state(
  comment_id INTEGER NOT NULL, iteration_id INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
  resolved_by TEXT, resolved_at REAL, PRIMARY KEY (comment_id, iteration_id));
"""


def _cc():
    es._c()  # events/iterations tables
    db.ensure(_DDL)
    return db.get()


def _short(body):
    return body if len(body) <= 80 else body[:77] + "..."


def _ctx(iteration_id):
    """(project_id, tape_id) for an iteration, or ValueError if it does not exist."""
    it = es.get_iteration(iteration_id)
    if it is None:
        raise ValueError(f"no iteration {iteration_id}")
    return es.get_tape(it["tape_id"])["project_id"], it["tape_id"]


def _comment(id):
    return _one("SELECT * FROM comments WHERE id=?", (id,))


def add_comment(iteration_id, author, kind, body, start_s=None, end_s=None, now=None):
    now = time.time() if now is None else now
    if kind not in KINDS:
        raise ValueError(f"unknown comment kind {kind!r}")
    body = (body or "").strip()
    if not body:
        raise ValueError("comment body is empty")
    if start_s is not None and start_s < 0:
        raise ValueError("start_s must be >= 0")
    if end_s is not None and (start_s is None or end_s <= start_s):
        raise ValueError("end_s needs a start_s and must be after it")
    _cc()
    project_id, tape_id = _ctx(iteration_id)
    with db.transaction() as conn:
        cid = conn.execute(
            "INSERT INTO comments(iteration_id, author, kind, start_s, end_s, body, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?)", (iteration_id, author, kind, start_s, end_s, body, now, now)).lastrowid
        conn.execute("INSERT INTO comment_state(comment_id, iteration_id) VALUES(?,?)", (cid, iteration_id))
        es.record_event(author, "comment_added", f"{author} commented: {_short(body)}", project_id=project_id,
                        tape_id=tape_id, iteration_id=iteration_id, comment_id=cid, now=now)
    return _comment(cid)


def edit_comment(id, author, *, body=None, kind=None, now=None):
    now = time.time() if now is None else now
    _cc()
    c = _comment(id)
    if c is None:
        raise ValueError(f"no comment {id}")
    if c["author"] != author:
        raise PermissionError("only the author can edit a comment")
    if kind is not None and kind not in KINDS:
        raise ValueError(f"unknown comment kind {kind!r}")
    if body is not None and not body.strip():
        raise ValueError("comment body is empty")
    cols = {k: v for k, v in (("body", body and body.strip()), ("kind", kind)) if v is not None}
    return es._set("comments", id, now, **cols)


def add_reply(comment_id, author, body, now=None):
    now = time.time() if now is None else now
    body = (body or "").strip()
    if not body:
        raise ValueError("reply body is empty")
    _cc()
    c = _comment(comment_id)
    if c is None:
        raise ValueError(f"no comment {comment_id}")
    project_id, tape_id = _ctx(c["iteration_id"])
    with db.transaction() as conn:
        rid = conn.execute("INSERT INTO replies(comment_id, author, body, created_at) VALUES(?,?,?,?)",
                           (comment_id, author, body, now)).lastrowid
        es.record_event(author, "reply_added", f"{author} replied: {_short(body)}", project_id=project_id,
                        tape_id=tape_id, iteration_id=c["iteration_id"], comment_id=comment_id, now=now)
    return _one("SELECT * FROM replies WHERE id=?", (rid,))


def set_resolved(comment_id, iteration_id, resolved, by, now=None):
    now = time.time() if now is None else now
    _cc()
    if _one("SELECT 1 AS x FROM comment_state WHERE comment_id=? AND iteration_id=?",
            (comment_id, iteration_id)) is None:
        raise ValueError("comment is not on that iteration")
    project_id, tape_id = _ctx(iteration_id)
    with db.transaction() as conn:
        conn.execute("UPDATE comment_state SET resolved=?, resolved_by=?, resolved_at=?"
                     " WHERE comment_id=? AND iteration_id=?",
                     (1, by, now, comment_id, iteration_id) if resolved else (0, None, None, comment_id, iteration_id))
        if resolved:
            es.record_event(by, "comment_resolved", f"{by} resolved a comment", project_id=project_id,
                            tape_id=tape_id, iteration_id=iteration_id, comment_id=comment_id, now=now)
    return _one("SELECT * FROM comment_state WHERE comment_id=? AND iteration_id=?", (comment_id, iteration_id))


def start_over(iteration_id, by, now=None) -> int:
    """Resolves every open comment on this iteration only (earlier versions keep their state)."""
    now = time.time() if now is None else now
    _cc()
    project_id, tape_id = _ctx(iteration_id)
    with db.transaction() as conn:
        n = conn.execute("UPDATE comment_state SET resolved=1, resolved_by=?, resolved_at=?"
                         " WHERE iteration_id=? AND resolved=0", (by, now, iteration_id)).rowcount
        es.record_event(by, "start_over", f"{by} started over, resolving {n} comment{'' if n == 1 else 's'}",
                        project_id=project_id, tape_id=tape_id, iteration_id=iteration_id, now=now)
    return n


def comments_for_iteration(iteration_id):
    _cc()
    rows = _all(
        "SELECT c.*, s.resolved, s.resolved_by, s.resolved_at, c.iteration_id AS origin_iteration_id,"
        " oi.version AS origin_version FROM comment_state s JOIN comments c ON c.id=s.comment_id"
        " JOIN iterations oi ON oi.id=c.iteration_id WHERE s.iteration_id=? ORDER BY c.created_at, c.id",
        (iteration_id,))
    for r in rows:
        r["resolved"] = bool(r["resolved"])
        r["carried"] = r["origin_iteration_id"] != iteration_id
        r["replies"] = _all("SELECT * FROM replies WHERE comment_id=? ORDER BY created_at, id", (r["id"],))
    return rows


def carry_over(iteration_id, now=None):
    _cc()
    it = es.get_iteration(iteration_id)
    if it is None:
        raise ValueError(f"no iteration {iteration_id}")
    prev = es.previous_ready_iteration(it["tape_id"], it["version"])
    if prev is None:
        return 0
    with db.transaction() as conn:
        return conn.execute(
            "INSERT OR IGNORE INTO comment_state(comment_id, iteration_id)"
            " SELECT comment_id, ? FROM comment_state WHERE iteration_id=? AND resolved=0",
            (iteration_id, prev["id"])).rowcount


def search(query, limit=50):
    q = (query or "").strip().lower()
    if not q:
        return []
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    _cc()
    base = ("SELECT '{k}' AS kind, c.id AS comment_id, c.iteration_id, i.tape_id, t.project_id, i.version,"
            " {b}.body AS snippet, c.start_s, {b}.created_at AS at FROM {src}"
            " JOIN iterations i ON i.id=c.iteration_id JOIN tapes t ON t.id=i.tape_id"
            " WHERE lower({b}.body) LIKE ? ESCAPE '\\'")
    sql = (base.format(k="comment", b="c", src="comments c") + " UNION ALL " +
           base.format(k="reply", b="r", src="replies r JOIN comments c ON c.id=r.comment_id") +
           " ORDER BY at DESC, comment_id DESC LIMIT ?")
    hits = _all(sql, (like, like, limit))
    for h in hits:
        h["snippet"] = h["snippet"][:SNIPPET_CHARS]
        del h["at"]
    return hits
