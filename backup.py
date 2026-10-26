"""Nightly copy of app.db to the Drive Backups folder, keeping the newest 7."""
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import db
import gdrive
import gdrive_oauth
from logger import get_logger

log = get_logger("backup")

BACKUP_HOUR = 4
BACKUP_KEEP = 7
_NAME = re.compile(r"^app-\d{4}-\d{2}-\d{2}\.db$")


async def run_if_due(now: float) -> None:
    try:
        tm = time.localtime(now)
        today = time.strftime("%Y-%m-%d", tm)
        if tm.tm_hour < BACKUP_HOUR or db.meta_get("backup_last_date") == today:
            return
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.db"
            dest = sqlite3.connect(path)
            try:
                db.get().backup(dest)  # consistent copy incl. WAL; fast for this size
            finally:
                dest.close()
            folder = db.meta_get("gdrive_backups")
            if not folder:
                await gdrive_oauth.ensure_layout()
                folder = db.meta_get("gdrive_backups")
            await gdrive.upload_small_file(path, f"app-{today}.db", folder, "application/x-sqlite3")
        db.meta_set("backup_last_date", today)
        log.info(f"backup app-{today}.db uploaded")
        try:
            # App-internal backups: deleted directly, no two-step undo window.
            names = sorted((f for f in await gdrive.list_folder(folder) if _NAME.match(f.get("name", ""))),
                           key=lambda f: f["name"])
            for f in names[:-BACKUP_KEEP]:
                await gdrive.delete_file(f["id"])
        except Exception as e:
            log.warning(f"backup prune failed: {e}")
    except Exception as e:
        log.error(f"backup failed: {e}")
