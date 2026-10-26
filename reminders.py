"""Hourly 'delete it from the editor page' DMs, 12:00-22:00 local, from 3h after the editor's download click."""
import time

import db
import deletions
import editor_store as es
import notify_outbox
from logger import get_logger

log = get_logger(__name__)

REMINDER_DELAY_SECONDS = 3 * 3600
REMINDER_EVERY_SECONDS = 3600
REMINDER_START_HOUR = 12
REMINDER_END_HOUR = 22
_PREFIX = "reminder_next_"


def _active(now) -> list[dict]:
    out = []
    for p in es.list_projects():  # excludes deleted_at projects
        if p["download_clicked_at"] is not None and p["vod_status"] == "ready" \
                and not deletions.has_live_request("project", p["id"]):
            out.append(p)
    return out


async def run(now: float) -> None:
    try:
        notify_outbox.ensure_schema()
        deletions._c()
        active = _active(now)
        live = {f"{_PREFIX}{p['id']}" for p in active}
        for r in db.get().execute("SELECT key FROM meta WHERE key LIKE ?", (_PREFIX + "%",)).fetchall():
            if r["key"] not in live:
                db.meta_delete(r["key"])
        if not REMINDER_START_HOUR <= time.localtime(now).tm_hour < REMINDER_END_HOUR:
            return
        for p in active:
            try:
                key = f"{_PREFIX}{p['id']}"
                nxt = db.meta_get(key)
                nxt = float(nxt) if nxt is not None else p["download_clicked_at"] + REMINDER_DELAY_SECONDS
                if now < nxt:
                    continue
                what = "clip" if p["kind"] == "clip" else "VOD"
                text = (f"Reminder: you downloaded {p['name']} — delete it from the editor page "
                        f"so it stops using Drive space." if what == "VOD" else
                        f"Reminder: you downloaded the clip {p['name']} — delete it from the editor page "
                        f"so it stops using Drive space.")
                with db.transaction():
                    db.get().execute(  # a backlog from an outage must not arrive as a burst
                        "UPDATE dm_outbox SET status='dropped', last_error='superseded'"
                        " WHERE status='pending' AND dedupe_key LIKE ?", (f"reminder:{p['id']}:%",))
                    notify_outbox.enqueue("editor", text, f"reminder:{p['id']}:{int(now // 3600)}", now)
                    db.meta_set(key, str(now + REMINDER_EVERY_SECONDS))
            except Exception:
                log.exception("reminder failed for project %s", p.get("id"))
    except Exception:
        log.exception("reminders.run failed")
