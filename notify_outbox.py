"""
Durable outbox of Discord DMs. Every DM is written to SQLite first (unique dedupe_key), then
delivered by the scheduler with backoff, so a restart neither loses nor repeats a message.
"""
import time

import db
import discord_dm
from config import config
from logger import get_logger
from scheduler import backoff_seconds

log = get_logger("NotifyOutbox")

BATCH = 20
_ID_KEYS = {"editor": "editor_discord_user_id", "streamer": "streamer_discord_user_id"}
_DDL = """
CREATE TABLE IF NOT EXISTS dm_outbox(
  id INTEGER PRIMARY KEY,
  recipient_role TEXT NOT NULL CHECK (recipient_role IN ('streamer','editor')),
  content TEXT NOT NULL,
  dedupe_key TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','dropped')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL,
  last_error TEXT,
  created_at REAL NOT NULL,
  sent_at REAL
);
CREATE INDEX IF NOT EXISTS dm_outbox_due ON dm_outbox(status, next_attempt_at);
"""
_ERR_KEY = "dm_last_error"
_logged_roles: set[str] = set()


def _c():
    db.ensure(db._META_DDL)  # meta_set/meta_delete run inside transactions; ensure first
    db.ensure(_DDL)
    return db.get()


def ensure_schema() -> None:
    """Callers that enqueue inside their own transaction call this first: db.ensure raises inside one."""
    _c()


def enqueue(recipient_role: str, content: str, dedupe_key: str, now: "float | None" = None) -> bool:
    """False when dedupe_key already exists. One statement, no transaction of its own, so it joins the
    caller's. While notifications are off the row is stored 'dropped' (key kept): no stale replay later."""
    now = time.time() if now is None else now
    off = not config.get("discord_notifications_enabled", False)
    cur = _c().execute(
        "INSERT OR IGNORE INTO dm_outbox(recipient_role, content, dedupe_key, status, next_attempt_at,"
        " created_at, last_error) VALUES(?,?,?,?,?,?,?)",
        (recipient_role, content, dedupe_key, "dropped" if off else "pending", now, now,
         "notifications disabled" if off else None))
    return cur.rowcount == 1


def reset_stuck_sending() -> int:
    """Startup: 'sending' -> 'pending'. Accepted duplicate: Discord may have taken the message and the
    power died before the row was marked sent, so that one DM can arrive twice. A second path: our client
    timed out after Discord had accepted the message. Rare; losing a DM is worse."""
    conn = _c()
    with db.transaction():
        cur = conn.execute("UPDATE dm_outbox SET status='pending' WHERE status='sending'")
    return cur.rowcount


async def _deliver_one(row, now: float) -> None:
    conn = _c()
    role = row["recipient_role"]
    user_id = str(config.get(_ID_KEYS[role], "") or "").strip()
    if not user_id:
        if role not in _logged_roles:
            _logged_roles.add(role)
            log.info("No Discord id for %s; their DMs are dropped", role)
        with db.transaction():
            conn.execute("UPDATE dm_outbox SET status='dropped', last_error=? WHERE id=?",
                         (f"no Discord id for {role}", row["id"]))
        return
    with db.transaction():  # conditional claim: an overlapping deliver_due must not send it too
        claimed = conn.execute("UPDATE dm_outbox SET status='sending' WHERE id=? AND status='pending'",
                               (row["id"],)).rowcount
    if not claimed:
        return
    try:
        await discord_dm.send_dm(user_id, row["content"])
    except Exception as e:
        known = isinstance(e, (discord_dm.DiscordError, discord_dm.DiscordNotConfigured))
        err = str(e)[:300] if known else type(e).__name__
        if not known:
            log.exception("Unexpected error sending DM row %s", row["id"])
        wait = max(backoff_seconds(row["attempts"]), getattr(e, "retry_after", None) or 0)
        with db.transaction():
            conn.execute("UPDATE dm_outbox SET status='pending', attempts=attempts+1, next_attempt_at=?,"
                         " last_error=? WHERE id=?", (now + wait, err, row["id"]))
            db.meta_set(_ERR_KEY, err)
        return
    with db.transaction():
        conn.execute("UPDATE dm_outbox SET status='sent', sent_at=?, last_error=NULL WHERE id=?",
                     (now, row["id"]))
        db.meta_delete(_ERR_KEY)


async def deliver_due(now: float) -> None:
    """Scheduler job. Never raises."""
    try:
        if not config.get("discord_notifications_enabled", False):
            with db.transaction():  # rows pending at switch-off must not replay when it is switched back on
                _c().execute("UPDATE dm_outbox SET status='dropped', last_error='notifications disabled'"
                             " WHERE status='pending'")
            return
        rows = _c().execute(
            "SELECT * FROM dm_outbox WHERE status='pending' AND next_attempt_at<=? ORDER BY id LIMIT ?",
            (now, BATCH)).fetchall()
    except Exception:
        log.exception("Could not read the DM outbox")
        return
    for row in rows:
        try:
            await _deliver_one(row, now)
        except Exception:
            log.exception("DM outbox row %s failed", row["id"])


def status() -> dict:
    conn = _c()

    def n(s):
        return conn.execute("SELECT COUNT(*) FROM dm_outbox WHERE status=?", (s,)).fetchone()[0]
    last = conn.execute("SELECT MAX(sent_at) FROM dm_outbox").fetchone()[0]
    return {"enabled": bool(config.get("discord_notifications_enabled", False)),
            "pending": n("pending"), "sending": n("sending"), "dropped": n("dropped"),
            "last_error": db.meta_get(_ERR_KEY), "last_sent_at": last}
