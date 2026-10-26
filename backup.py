"""Nightly copy of app.db to the Drive Backups folder, keeping the newest 7.

Does nothing at all until Drive is connected, and after a failure waits
scheduler.backoff_seconds(attempts) before trying again, so a disconnected or
broken Drive costs neither a DB copy nor an ERROR line every 30 s tick.
"""
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import db
import gdrive
import gdrive_oauth
import scheduler
from config import config
from logger import get_logger

log = get_logger("Backup")

BACKUP_HOUR = 4
BACKUP_KEEP = 7
_NAME = re.compile(r"^app-\d{4}-\d{2}-\d{2}\.db$")

_not_connected_logged = False


def _retry_later(now: float) -> None:
    attempts = int(db.meta_get("backup_attempts") or 0)
    db.meta_set("backup_next_try", str(now + scheduler.backoff_seconds(attempts)))
    db.meta_set("backup_attempts", str(attempts + 1))


async def run_if_due(now: float) -> None:
    global _not_connected_logged
    if not config.get("gdrive_refresh_token", ""):
        return
    try:
        tm = time.localtime(now)
        today = time.strftime("%Y-%m-%d", tm)
        if tm.tm_hour < BACKUP_HOUR or db.meta_get("backup_last_date") == today:
            return
        next_try = float(db.meta_get("backup_next_try") or 0)
        # More than the longest backoff ahead means it was stamped by a clock that later moved back.
        if now < next_try <= now + scheduler.backoff_seconds(99):
            return
        folder = db.meta_get("gdrive_backups")
        if not folder:
            await gdrive_oauth.ensure_layout()
            folder = db.meta_get("gdrive_backups")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            dest = sqlite3.connect(path)
            try:
                db.get().backup(dest)  # consistent copy incl. WAL; fast for this size
            finally:
                dest.close()
            await gdrive.upload_small_file(path, f"app-{today}.db", folder, "application/x-sqlite3")
        db.meta_set("backup_last_date", today)
        db.meta_set("backup_attempts", "0")
        db.meta_set("backup_next_try", "0")
        log.info(f"backup app-{today}.db uploaded")
        try:
            # App-internal backups: deleted directly, no two-step undo window.
            names = sorted((f for f in await gdrive.list_folder(folder) if _NAME.match(f.get("name", ""))),
                           key=lambda f: f["name"])
            for f in names[:-BACKUP_KEEP]:
                await gdrive.delete_file(f["id"])
        except Exception as e:
            log.warning(f"backup prune failed: {e}")
    except gdrive.DriveNotConnected as e:
        if not _not_connected_logged:
            _not_connected_logged = True
            log.info(f"backup skipped until Drive is reconnected: {e}")
        _retry_later(now)
    except Exception as e:
        log.error(f"backup failed: {e}")
        _retry_later(now)
