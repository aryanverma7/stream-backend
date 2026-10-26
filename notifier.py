"""
Turns the editor app's events log into Discord DMs via the outbox. A VOD/clip or a new version
is DMed at once; comments, replies, resolves and start-overs collect in digest_pending and go to
the other person as one digest once the actor has been quiet for DIGEST_QUIET_SECONDS.
"""
import db
import editor_store as es
import notify_outbox
from auth import role_for_username
from logger import get_logger

log = get_logger("Notifier")

DIGEST_QUIET_SECONDS = 600
OUTAGE_SECONDS = 600
_CURSOR = "notify_cursor"
_OTHER = {"admin": "editor", "editor": "streamer"}
_DIGEST_KINDS = ("comment_added", "reply_added", "comment_resolved", "start_over")
_boot_last_seen: "float | None" = None
_back_online_done = False
_DDL = """
CREATE TABLE IF NOT EXISTS digest_pending(
  recipient_role TEXT NOT NULL, event_id INTEGER NOT NULL UNIQUE, actor TEXT NOT NULL, at REAL NOT NULL);
"""


def display_name(username: str) -> str:
    return username


def _handle(ev: dict, now: float) -> None:
    kind, actor = ev["kind"], ev["actor"]
    if kind == "vod_ready":
        p = es.get_project(ev["project_id"])
        if p is None:  # row gone: skip it, never wedge the cursor on it
            log.warning("Event %s: row gone, skipped", ev["id"])
            return
        what = "clip" if p["kind"] == "clip" else "VOD"
        notify_outbox.enqueue("editor", f"New {what} ready: {p['name']}. Open the editor page to download it.",
                              f"vod_ready:{p['id']}", now)
    elif kind == "iteration_ready":
        if actor == "system" or role_for_username(actor) == "editor":
            it = es.get_iteration(ev["iteration_id"])
            tape = it and es.get_tape(it["tape_id"])
            if tape is None:
                log.warning("Event %s: row gone, skipped", ev["id"])
                return
            notify_outbox.enqueue("streamer", f"{tape['name']} v{it['version']} is ready to review.",
                                  f"iteration_ready:{it['id']}", now)
    elif kind in _DIGEST_KINDS:
        recipient = _OTHER.get(role_for_username(actor))
        if recipient:
            db.get().execute("INSERT OR IGNORE INTO digest_pending(recipient_role, event_id, actor, at)"
                             " VALUES(?,?,?,?)", (recipient, ev["id"], actor, ev["at"]))


def _count(kind: str, n: int) -> str:
    if kind == "comment_added":
        return f"{n} comment{'' if n == 1 else 's'}"
    if kind == "reply_added":
        return f"{n} repl{'y' if n == 1 else 'ies'}"
    if kind == "comment_resolved":
        return f"{n} resolved"
    return "started over" if n == 1 else f"started over {n} times"


def label(tape_id, iteration_id) -> str:
    tape = es.get_tape(tape_id) if tape_id is not None else None
    it = es.get_iteration(iteration_id) if iteration_id is not None else None
    name = tape["name"] if tape else "a deleted tape"
    return f"{name} v{it['version']}" if it else name


def _digest(rows) -> str:
    """rows in event order: actor -> (tape, iteration) -> kind -> count, each in first-seen order."""
    by_actor: dict[str, dict[tuple, dict[str, int]]] = {}
    for r in rows:
        group = by_actor.setdefault(r["actor"], {}).setdefault((r["tape_id"], r["iteration_id"]), {})
        group[r["kind"]] = group.get(r["kind"], 0) + 1
    lines = []
    for actor, groups in by_actor.items():
        parts = [", ".join(_count(k, c[k]) for k in _DIGEST_KINDS if k in c) + " on " + label(*key)
                 for key, c in groups.items()]
        lines.append(f"{display_name(actor)}: " + "; ".join(parts))
    return "; ".join(lines) + "."


def _flush(now: float) -> None:
    conn = db.get()
    due = conn.execute("SELECT recipient_role, MAX(event_id) FROM digest_pending GROUP BY recipient_role"
                       " HAVING MAX(at) <= ?", (now - DIGEST_QUIET_SECONDS,)).fetchall()
    for recipient, max_id in due:
        try:
            with db.transaction():
                rows = conn.execute(
                    "SELECT d.actor, e.kind, e.tape_id, e.iteration_id FROM digest_pending d"
                    " JOIN events e ON e.id=d.event_id WHERE d.recipient_role=? AND d.event_id<=?"
                    " ORDER BY d.event_id", (recipient, max_id)).fetchall()
                conn.execute("DELETE FROM digest_pending WHERE recipient_role=? AND event_id<=?",
                             (recipient, max_id))
                if rows:
                    notify_outbox.enqueue(recipient, _digest(rows), f"digest:{recipient}:{max_id}", now)
        except Exception:
            log.exception("Digest for %s failed", recipient)


async def run(now: float) -> None:
    """Scheduler job. Never raises."""
    try:
        # Every schema before any transaction: db.ensure raises inside one.
        notify_outbox.ensure_schema()
        es._c()
        db.ensure(_DDL)
        raw = db.meta_get(_CURSOR)
        if raw is None:  # first run on an existing database: don't DM history
            raw = str(es._c().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
            with db.transaction():
                db.meta_set(_CURSOR, raw)
        cursor = int(raw)
        for ev in es.events_since(cursor):
            try:
                with db.transaction():
                    _handle(ev, now)
                    db.meta_set(_CURSOR, str(ev["id"]))
            except Exception:
                log.exception("Event %s could not be processed; retrying next tick", ev["id"])
                break
        _flush(now)
    except Exception:
        log.exception("Notifier run failed")


def set_boot_last_seen(prev: "float | None") -> None:
    """main.py passes scheduler.last_seen() read BEFORE scheduler.start(): the first tick overwrites it."""
    global _boot_last_seen
    _boot_last_seen = prev


async def back_online_once(now: float) -> None:
    """Scheduler job (so only on sane ticks), effective once per process: after an outage longer
    than OUTAGE_SECONDS the editor is told the site is back. The key dedupes a crash-loop of restarts."""
    global _back_online_done
    if _back_online_done:
        return
    prev = _boot_last_seen
    if prev is not None and now - prev > OUTAGE_SECONDS:
        notify_outbox.enqueue(
            "editor", f"The editor site is back online (it was down for about {round((now - prev) / 60)} min).",
            f"back_online:{int(prev)}", now)
    _back_online_done = True  # after enqueue: a failed write retries next tick


def _reset_for_tests() -> None:
    global _boot_last_seen, _back_online_done
    _boot_last_seen = None
    _back_online_done = False
