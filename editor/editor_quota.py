"""The editor's weekly quota: Final videos, and Final reels & clips (one short-form target, key "reels"),
per Monday-Sunday week, local time.

An edit counts once it is Final and not quota_exempt, in the week its Final version was *uploaded*
(edits.final_upload_at, set by editor_store.update_edit), so a late approval never costs the editor;
removing Final removes the credit. Each week stands alone (no carry-over). Targets apply from the week
they were set (quota_targets, newest row at or before a week wins), so past weeks keep theirs. Tracking
starts at meta quota_start_week (the first week the quota was looked at, changeable by the streamer).
A week the streamer marks excused is never "missed".

run() is the scheduler job for the two DMs: Friday from 18:00 to the editor while the week is behind,
Monday from 09:00 to the streamer with last week's line. Both dedupe per week, so a tick that comes late
(the Mac Mini was off) still sends once, and neither replays.
"""
import math
import time
from datetime import date, timedelta

from core import db
from core.config import config
from core.logger import get_logger
from editor import editor_store as es
from editor import notify_outbox

log = get_logger(__name__)

DEFAULT_VIDEOS = 2
DEFAULT_REELS = 10
MAX_TARGET = 100
FRIDAY_HOUR = 18
BAD_WEEK = "week must be a Monday, written YYYY-MM-DD"
MONDAY_HOUR = 9

_DDL = """
CREATE TABLE IF NOT EXISTS quota_targets(week TEXT PRIMARY KEY, videos INTEGER NOT NULL, reels INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quota_excused(week TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '');
"""


def _c():
    es._c()
    db.ensure(_DDL)
    db.ensure(db._META_DDL)
    return db.get()


# -- weeks: "YYYY-MM-DD" of the Monday, local time --

def week_of(ts: float) -> str:
    d = date.fromtimestamp(ts)
    return (d - timedelta(days=d.weekday())).isoformat()


def parse_week(week: str) -> str:
    """ValueError unless it is a Monday written as YYYY-MM-DD."""
    try:
        d = date.fromisoformat(str(week))
    except ValueError:
        d = None
    if d is None or d.weekday() != 0 or d.isoformat() != week:
        raise ValueError(BAD_WEEK)
    return week


def _next(week: str) -> str:
    return (date.fromisoformat(week) + timedelta(days=7)).isoformat()


def week_start_ts(week: str) -> float:
    return time.mktime(date.fromisoformat(week).timetuple())


# -- settings --

def start_week(now: float) -> str:
    _c()
    week = db.meta_get("quota_start_week")
    if not week:
        week = week_of(now)
        db.meta_set("quota_start_week", week)
    return week


def set_start_week(week: str, now: float) -> str:
    parse_week(week)
    if week > week_of(now):
        raise ValueError("The start week can't be in the future")
    _c()
    db.meta_set("quota_start_week", week)
    return week


def targets_for(week: str) -> dict:
    row = _c().execute("SELECT videos, reels FROM quota_targets WHERE week<=? ORDER BY week DESC LIMIT 1",
                       (week,)).fetchone()
    return {"videos": row["videos"], "reels": row["reels"]} if row else {"videos": DEFAULT_VIDEOS, "reels": DEFAULT_REELS}


def set_targets(videos: int, reels: int, now: float) -> dict:
    """From this week on; earlier weeks keep what they had."""
    for n in (videos, reels):
        if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= MAX_TARGET:
            raise ValueError(f"Targets must be whole numbers from 0 to {MAX_TARGET}")
    conn = _c()
    with db.transaction():
        conn.execute("INSERT OR REPLACE INTO quota_targets(week, videos, reels) VALUES(?,?,?)",
                     (week_of(now), videos, reels))
    return targets_for(week_of(now))


def set_excused(week: str, excused: bool, note: str = "") -> None:
    parse_week(week)
    conn = _c()
    with db.transaction():
        if excused:
            conn.execute("INSERT OR REPLACE INTO quota_excused(week, note) VALUES(?,?)", (week, note.strip()[:200]))
        else:
            conn.execute("DELETE FROM quota_excused WHERE week=?", (week,))


# -- the table --

def _credited(since: float) -> list:
    return [dict(r) for r in _c().execute(
        "SELECT t.id, t.name, t.kind, t.project_id, p.name AS project_name, t.final_upload_at"
        " FROM edits t JOIN projects p ON p.id=t.project_id"
        " WHERE t.status='final' AND t.quota_exempt=0 AND t.final_upload_at IS NOT NULL AND t.final_upload_at>=?"
        " ORDER BY t.final_upload_at", (since,)).fetchall()]


def summary(now: "float | None" = None) -> dict:
    """Every week from the start week to this one, newest first."""
    now = time.time() if now is None else now
    start, current = start_week(now), week_of(now)
    excused = {r["week"]: r["note"] for r in _c().execute("SELECT week, note FROM quota_excused").fetchall()}
    by_week: dict = {}
    for t in _credited(week_start_ts(start)):
        by_week.setdefault(week_of(t["final_upload_at"]), []).append(t)
    weeks, w = [], start
    while w <= current:
        target = targets_for(w)
        edits = by_week.get(w, [])
        done = {"videos": sum(t["kind"] == "video" for t in edits),
                "reels": sum(t["kind"] in ("reel", "clip") for t in edits)}
        met = all(done[k] >= target[k] for k in target)
        if met:
            status = "met"
        elif w in excused:
            status = "excused"
        else:
            status = "in_progress" if w == current else "missed"
        weeks.append({"week": w, "target": target, "done": done,
                      "short": {k: max(0, target[k] - done[k]) for k in target},
                      "status": status, "note": excused.get(w, ""), "excused": w in excused, "edits": edits})
        w = _next(w)
    return {"start_week": start, "current_week": current, "week_ends_at": week_start_ts(_next(current)),
            "targets": targets_for(current), "weeks": weeks[::-1]}


# -- the DMs --

_LABELS = {"videos": ("video", "videos"), "reels": ("reel or clip", "reels & clips")}


def _counts(wk: dict) -> str:
    return (f"videos {wk['done']['videos']}/{wk['target']['videos']}, "
            f"reels & clips {wk['done']['reels']}/{wk['target']['reels']}")


def _to_go(short: dict) -> str:
    parts = [f"{n} {_LABELS[k][n != 1]}" for k, n in short.items() if n]
    return " and ".join(parts)


def friday_text(wk: dict, days_left: int) -> str:
    return (f"Quota this week: {_counts(wk)}. {_to_go(wk['short'])} to go, "
            f"{days_left} day{'' if days_left == 1 else 's'} left (the week ends Sunday night).")


def monday_text(wk: dict) -> str:
    end = (date.fromisoformat(wk["week"]) + timedelta(days=6)).strftime("%d %b")
    head = f"Last week's quota ({date.fromisoformat(wk['week']).strftime('%d %b')} - {end}): {_counts(wk)}"
    if wk["status"] == "met":
        return f"{head}. Met."
    if wk["status"] == "excused":
        return f"{head}. Excused{': ' + wk['note'] if wk['note'] else ''}."
    return f"{head}. Missed by {_to_go(wk['short'])}."


async def run(now: float) -> None:
    try:
        notify_outbox.ensure_schema()
        s = summary(now)
        lt = time.localtime(now)
        this = s["weeks"][0]
        if config.get("editor_quota_friday_reminder", True) and (lt.tm_wday > 4 or (lt.tm_wday == 4 and lt.tm_hour >= FRIDAY_HOUR)) \
                and this["status"] == "in_progress":
            days = max(1, math.ceil((s["week_ends_at"] - now) / 86400))
            notify_outbox.enqueue("editor", friday_text(this, days), f"quota_friday:{this['week']}", now)
        if config.get("editor_quota_monday_summary", True) and (lt.tm_wday > 0 or lt.tm_hour >= MONDAY_HOUR) \
                and len(s["weeks"]) > 1:
            last = s["weeks"][1]
            notify_outbox.enqueue("streamer", monday_text(last), f"quota_monday:{last['week']}", now)
    except Exception:
        log.exception("quota reminders failed")
