"""
Recovery: finish uploads the editor's browser completed while we were off, or
whose /complete call never arrived. A scheduler job: runs on the first sane
tick after boot, then only while an iteration is `uploading`, at most every
RECONCILE_EVERY_SECONDS.
"""
import time
from collections import defaultdict

from core import db
from editor import editor_service
from editor import editor_store as es
from editor import gdrive
from core import scheduler
from core.logger import get_logger

log = get_logger("Reconcile")

ABANDON_AFTER_SECONDS = 8 * 86400
RECONCILE_EVERY_SECONDS = 600
_LAST_RUN_KEY = "reconcile_last_run"

_ran_since_boot = False


async def run_if_due(now: float) -> None:
    global _ran_since_boot
    if _ran_since_boot:
        if not es.any_uploading():
            return
        since = now - float(db.meta_get(_LAST_RUN_KEY) or 0)
        if 0 <= since < RECONCILE_EVERY_SECONDS:  # negative: stamped by a clock that later moved back
            return
    _ran_since_boot = True
    db.meta_set(_LAST_RUN_KEY, str(now))
    await run_once(now)


async def run_once(now: "float | None" = None) -> dict:
    now = time.time() if now is None else now
    counts = {"completed": 0, "abandoned": 0}
    try:
        by_tape = defaultdict(list)
        for p in es.list_projects():
            for t in es.list_tapes(p["id"]):
                for it in es.list_iterations(t["id"]):
                    if it["status"] == "uploading":
                        by_tape[t["id"]].append(it)
        for tape_id, its in by_tape.items():
            folder = (es.get_tape(tape_id) or {}).get("drive_folder_id")
            files = None
            if folder:
                try:
                    files = await gdrive.list_folder(folder)
                except gdrive.DriveError as e:
                    log.warning(f"reconcile: cannot list tape {tape_id}: {e}")
            for it in its:
                found = None if files is None else next(
                    (f for f in files if f.get("appProperties", {}).get("iteration_id") == str(it["id"])), None)
                if found:
                    try:
                        await editor_service.complete_iteration_upload(it["id"], found["id"], "system")
                        counts["completed"] += 1
                    except (ValueError, gdrive.DriveError) as e:
                        log.warning(f"reconcile: iteration {it['id']} not completed: {e}")
                elif (files is not None and now - max(it["created_at"], it["updated_at"]) > ABANDON_AFTER_SECONDS
                      and scheduler.clock_is_sane(now)):
                    if es.abandon_iteration(it["id"], now):
                        counts["abandoned"] += 1
            # Unknown files in the folder are left alone, never deleted.
    except gdrive.DriveNotConnected:
        pass  # nothing to reconcile against until Drive is connected; the next due run tries again
    except Exception as e:  # a scheduler job must not fail on recovery
        log.error(f"reconcile failed: {e}")
    return counts
