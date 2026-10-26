"""Boot-time recovery: finish uploads the editor's browser completed while we were off."""
import time
from collections import defaultdict

import editor_service
import editor_store as es
import gdrive
import scheduler
from logger import get_logger

log = get_logger("Reconcile")

ABANDON_AFTER_SECONDS = 8 * 86400


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
                except (gdrive.DriveError, gdrive.DriveNotConnected) as e:
                    log.warning(f"reconcile: cannot list tape {tape_id}: {e}")
            for it in its:
                found = None if files is None else next(
                    (f for f in files if f.get("appProperties", {}).get("iteration_id") == str(it["id"])), None)
                if found:
                    try:
                        await editor_service.complete_iteration_upload(it["id"], found["id"], "system")
                        counts["completed"] += 1
                    except ValueError as e:
                        log.warning(f"reconcile: iteration {it['id']} not completed: {e}")
                elif (files is not None and now - it["created_at"] > ABANDON_AFTER_SECONDS
                      and scheduler.clock_is_sane(now)):
                    es._set("iterations", it["id"], now, status="abandoned")
                    counts["abandoned"] += 1
            # Unknown files in the folder are left alone, never deleted.
    except Exception as e:  # boot must not fail on recovery
        log.error(f"reconcile failed: {e}")
    return counts
