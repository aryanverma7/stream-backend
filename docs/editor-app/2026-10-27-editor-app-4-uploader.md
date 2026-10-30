# Editor App Plan 4: Gaming PC Uploader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Right-click one or more video files on the gaming PC, choose "Send to editor", and each one uploads to Drive by itself (surviving power cuts, pausing while live), becomes a project (a clip if under 5 minutes), and the editor is told it is ready.

**Architecture:** Backend gets a small secret-gated `/api/uploader` API (create-or-resume a project and its Drive resumable session, complete it, report the live level), and `vod_ready` starts verifying the Drive file. The gaming PC gets a windowless `pythonw` uploader: each right-click drops a job file in a queue folder, one worker (file lock) drains it with Google's resumable-upload protocol straight to Drive, under a tray icon. The PC never holds a Google credential: the backend hands it only the session URI, as it does for the editor's browser.

**Tech Stack:** Backend: Python 3.14, aiohttp, stdlib sqlite3 (existing). Gaming PC: Python on Windows, `requests` (already a dependency), new `pystray`, `pymediainfo`, `obsws-python`; stdlib `winreg`, `msvcrt`, `ctypes`.

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md` ("Flow 1", "Clips and reels", "Decisions made after the builder check": the "Live" bullet). Roadmap rows: "Gaming PC uploader" and the Plan 4 row under "Clips and reels".

## Global Constraints

- Secret: the existing `ocr_agent_secret` (backend) = `agent_secret` (gaming PC `agent_config.json`), sent as `X-Agent-Secret`; one machine, one trust boundary. Compare with `hmac.compare_digest`. No new backend config keys.
- Every new machine-to-machine route goes in `auth.py` `OPEN_PATHS` (add `"/api/uploader"`; matching is whole-segment). Not under `/api/editor` (that prefix needs a session).
- Bytes never pass through the Mac Mini. Clip rule: `duration_s < editor_store.CLIP_MAX_SECONDS` (300), decided only by `create_project(duration_s=...)`.
- `vod_ready` actor is `"agent"`, never a username (the bell and digests skip the actor's own events).
- Resumable chunks are multiples of 256 KiB: `CHUNK = 8 * 1024 * 1024`.
- Pause while OBS is streaming; resume `RESUME_AFTER_SECONDS = 300` after it stops. OBS unreachable: ask the backend; neither answers: upload.
- Never hold a DB transaction across an `await`; DDL before `db.transaction()`. Timing code takes `now`/clock injection.
- Gaming PC files live beside `agent.py`, tests beside the source (`test_*.py`), `requirements.txt` uses `>=` floors (see its header comment). Heavy/Windows-only imports (`pystray`, `pymediainfo`, `obsws_python`, `winreg`, `msvcrt`) happen inside functions so the tests import cleanly on Linux.
- Commits: `-c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com`, stage files by name (`*:Zone.Identifier` files are not ignored), attribution trailers per the session.
- Backend tests (WSL, from `mac mini/Stream`): `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings` (the 12 `test_credit_ocr.py` failures are pre-existing). Gaming PC tests (from `gaming pc/`): `uv run --no-project --python 3.14 --with requests --with pytest python -m pytest test_drive_upload.py test_live_gate.py test_uploader.py test_install_uploader.py -q`.

## Review Focus

1. Power cut between the backend creating the project and the PC saving the reply: the retry must get the same project back, not a second one (idempotent `job_id`). Pinned in Task 2.
2. Resumable session older than ~a week (Google answers 404/410): ask for a fresh session and restart from byte 0, never give up the job. Pinned in Task 3 and Task 5.
3. File moved, edited or deleted after it was queued: the job is dropped with a tray message, never uploads different bytes than the backend was told (size + mtime checked on every start/resume). Pinned in Task 5.
4. Eight files selected at once = eight processes started at once: exactly one worker, no job lost in the hand-over. Pinned in Task 5.
5. Mac Mini down while the PC uploads (or when the upload finishes): bytes keep going to Google; `complete` is retried until the backend answers, and repeating it is harmless. Pinned in Task 2 and Task 5.

---

### Task 1: Backend: live level and VOD file verification

**Files:**
- Modify: `mac mini/Stream/discord_live.py` (`poll_once`)
- Modify: `mac mini/Stream/editor_service.py` (`vod_ready`)
- Test: `mac mini/Stream/tests/test_discord_live.py`, `mac mini/Stream/tests/test_editor_service.py`

**Interfaces:**
- Produces: `discord_live.live_state(now: float | None = None) -> dict` = `{"live": bool | None, "title": str}`. `live` is `None` when no Twitch check succeeded in the last `3 * _poll_seconds()`; `title` is the title of the last poll that saw the channel live ("" if none), with `title_at` kept internally.
- Produces: `discord_live.last_live_title(on_date: str) -> str`: that title if the poll that saw it was on local date `on_date` (`YYYY-MM-DD`), else `""`.
- Changes: `editor_service.vod_ready(project_id, file_id, actor)` now refuses (`ValueError("That file is not this project's upload")`) unless `gdrive.get_file(file_id)` has the project's `drive_folder_id` in `parents`, `appProperties.project_id == str(project_id)` and `size == vod_size`. The already-`ready` early return stays first (a repeat is free and makes no Drive call).

- [ ] **Step 1: Write the failing tests**
  - `test_live_state_none_before_any_check`, `test_live_state_true_after_live_poll` (inject `stream_info` returning `{"title": "Ranked grind", ...}`; `live_state()["live"] is True`, title matches), `test_live_state_false_after_offline_poll` (`stream_info` returns `None` -> `False`), `test_live_state_goes_none_when_stale` (`now` past `3 * _poll_seconds()` -> `None`), `test_failed_poll_does_not_update_level` (raising `stream_info` leaves the previous value). Record the level even when announcements would dedupe (record before `announce`).
  - `test_last_live_title_only_for_same_local_date`.
  - `test_vod_ready_rejects_file_outside_project_folder`, `..._wrong_project_property`, `..._wrong_size` (each raises `ValueError`, project stays `uploading`, no share call); `test_vod_ready_accepts_matching_file_and_records_agent_event` (event actor `"agent"`); `test_vod_ready_repeat_makes_no_drive_call`.
- [ ] **Step 2: Run them, expect FAIL** (`live_state` undefined; `vod_ready` accepts any file).
- [ ] **Step 3: Implement.** Module globals `_live = {"live": None, "checked_at": 0.0, "title": "", "title_at": 0.0}`, written in `poll_once` after a successful `stream_info` call, reset in `reset()`. `poll_once` still only runs while announcements are on: say so in `live_state`'s docstring (`None` then, and the uploader treats `None` as "not live").
- [ ] **Step 4: Run the backend suite, expect PASS** (plus the 12 known OCR failures).
- [ ] **Step 5: Commit** `feat: live level for the uploader, verify the VOD file before vod_ready`.

### Task 2: Backend: `/api/uploader` routes

**Files:**
- Create: `mac mini/Stream/uploader_api.py`
- Modify: `mac mini/Stream/editor_api.py` (extract the exception mapping), `mac mini/Stream/auth.py` (`OPEN_PATHS`), `mac mini/Stream/server.py` (register next to `editor_api`, before `site_server`)
- Modify: `/home/aryan/projects/CLAUDE.md` (outside both repos: new subsection "Gaming-PC uploader", and the Plan 4 sentence in "What later plans build on")
- Test: `mac mini/Stream/tests/test_uploader_api.py`, one line in `tests/test_auth.py`

**Interfaces:**
- Consumes: Task 1; `es.create_project`, `es.get_project`, `es.set_vod_upload`, `svc.ensure_project_folder`, `svc.free_space`, `svc.vod_ready`, `gdrive.start_resumable_upload`, `deletions.ensure_project_not_deleting`, `db.meta_get/meta_set`.
- Produces: `editor_api.map_errors(handler)`: decorator wrapping `async (request) -> Response` with the existing `_Fail`/`DriveFull` 507/`DriveNotConnected` 503/`DriveError` 502/`NotFound` 404/`PermissionError` 403/`ValueError` 400 mapping; `_route` becomes session check + `map_errors`. Behaviour of every existing route unchanged.
- Produces (all need `X-Agent-Secret` == `ocr_agent_secret`, else `401 {"error": "Invalid or missing agent secret"}`; an empty configured secret refuses everything):
  - `POST /api/uploader/projects` body `{job_id, filename, size, mime, duration_s, stream_date, new_session?}` -> `200 {project_id, kind, status, upload_url}`. `job_id` is `[0-9a-f]{32}`; `size` int > 0; `duration_s` number or null; `stream_date` `YYYY-MM-DD`. Idempotent: `meta uploader_job_<job_id>` maps to the project id. First call: free-space check (`DriveFull` -> 507 before anything is created), `create_project(name=Path(filename).stem, stream_date, stream_title=discord_live.last_live_title(stream_date) or stem, vod_filename=filename, vod_size=size, duration_s=duration_s)`, meta write, then the session. A session is (re)created when the project's `vod_upload_url` is empty or `new_session` is true, and only while `vod_status == "uploading"`: `ensure_project_folder`, `ensure_project_not_deleting`, `start_resumable_upload(filename, folder, size, mime, {"project_id": str(id)}, None)`, `set_vod_upload`. A project already `ready` answers `status: "ready"`, `upload_url: ""`; `deleting`/`deleted` answers 409 `{"error": "This VOD was deleted"}`.
  - `POST /api/uploader/projects/{id}/complete` body `{file_id}` -> `200 {status: "ready"}` via `svc.vod_ready(id, file_id, "agent")`; 409 for deleting/deleted (the uploader drops the job).
  - `GET /api/uploader/live` -> `{"live": bool | None}` from `discord_live.live_state()`.

- [ ] **Step 1: Write the failing tests** (reuse `test_editor_api.py`'s fake-Drive fixture shape; `ocr_agent_secret` set in `config._data`):
  - `test_every_route_refuses_without_secret`, `test_refuses_when_secret_unset`, `test_open_paths_lets_uploader_through_without_session` (in `test_auth.py`: `path_matches("/api/uploader/live", ...)` reaches the handler).
  - `test_create_makes_project_and_session` (kind `vod` for `duration_s=3600`, `upload_url` returned, project `vod_upload_url` saved, session props `{"project_id": "<id>"}`).
  - `test_create_short_file_is_clip_with_reel` (`duration_s=42.5` -> `kind == "clip"`, `reel_tape_id` set).
  - `test_create_is_idempotent_by_job_id` (same `job_id` twice -> same `project_id`, one project, one session).
  - `test_create_retries_session_after_drive_failure` (first `start_resumable_upload` raises `DriveError` -> 502, project exists; second call -> same project, URL now set).
  - `test_new_session_replaces_url`, `test_ready_project_answers_ready_without_new_session`, `test_deleted_project_answers_409`.
  - `test_create_refuses_when_drive_full` (507, no project, no meta row).
  - `test_create_validates_body` (bad `job_id`, `size` 0, bad `stream_date` -> 400).
  - `test_complete_marks_ready_with_agent_actor`, `test_complete_twice_is_ok`, `test_complete_rejects_wrong_file` (400).
  - `test_live_reports_level` (monkeypatch `discord_live.live_state`).
  - Existing `test_editor_api.py` passes unchanged (the `map_errors` extraction).
- [ ] **Step 2: Run, expect FAIL** (404s).
- [ ] **Step 3: Implement** `uploader_api.py` (`_secret_ok(request) -> bool`, the three handlers, `register_routes(app)`), the `map_errors` extraction, `OPEN_PATHS` entry with a comment like its neighbours, registration in `server.py`.
- [ ] **Step 4: Run the backend suite, expect PASS.**
- [ ] **Step 5: Docs.** CLAUDE.md: a short "Gaming-PC uploader" subsection (routes, idempotent `job_id`, `appProperties.project_id`, actor `agent`, VOD uploads are still not reconciled because the uploader retries `complete` itself, live level is `None` while go-live announcements are off). Commit `feat: secret-gated uploader API for the gaming PC`.

### Task 3: Gaming PC: resumable upload protocol (`drive_upload.py`)

**Files:**
- Create: `gaming pc/drive_upload.py`, `gaming pc/test_drive_upload.py`

**Interfaces:**
- Produces: `class SessionExpired(Exception)`; `CHUNK = 8 * 1024 * 1024`;
  `query_offset(http, url: str, total: int) -> int | dict` (PUT, empty body, `Content-Range: bytes */{total}`: `308` -> next byte from `Range: bytes=0-N` (N+1) or 0 without `Range`; `200/201` -> the file JSON dict, i.e. already done; `404/410` -> `SessionExpired`; anything else -> `requests.HTTPError` via `raise_for_status`);
  `send_from(http, url: str, path: Path, total: int, offset: int, on_chunk: Callable[[int], bool]) -> dict` (reads `CHUNK` at a time from `offset`, PUT with `Content-Range: bytes {start}-{end}/{total}`; each `308` advances to the server's `Range`, not to what was sent; `on_chunk(next_offset)` is called after every chunk and returning `False` stops early, returning `{}`; `200/201` returns the file JSON (`id` is the Drive file id)). `http` is a `requests.Session`-like object (`.put(url, data=, headers=, timeout=)`), injected.

- [ ] **Step 1: Write the failing tests** with a fake `http` that records calls and replays canned responses:
  - `test_query_offset_parses_range` (`Range: bytes=0-8388607` -> 8388608), `test_query_offset_no_range_is_zero`, `test_query_offset_done_returns_file`, `test_query_offset_404_and_410_raise_session_expired`.
  - `test_send_from_sends_256k_aligned_chunks_with_content_range` (20 MiB temp file: headers `bytes 0-8388607/20971520`, `bytes 8388608-16777215/...`, last `bytes 16777216-20971519/...`; returns `{"id": "FILE"}`).
  - `test_send_from_follows_server_range_after_short_commit` (server answers 308 with a smaller `Range`; next chunk starts there).
  - `test_send_from_stops_when_on_chunk_false`, `test_send_from_starts_at_offset`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Timeouts `(10, 120)`. No retries here: the caller owns retry/backoff.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5:** No commit yet: `gaming pc/` has no `.git`; Task 6 ships all gaming-PC files together.

### Task 4: Gaming PC: live gate (`live_gate.py`)

**Files:**
- Create: `gaming pc/live_gate.py`, `gaming pc/test_live_gate.py`

**Interfaces:**
- Produces: `RESUME_AFTER_SECONDS = 300`, `RECHECK_SECONDS = 30`;
  `class LiveGate(obs_streaming: Callable[[], bool | None], backend_live: Callable[[], bool | None], clock=time.monotonic)` with `should_pause() -> bool` (asks at most once per `RECHECK_SECONDS`, else reuses the last answer; OBS first, the backend only when OBS answered `None`; `None` from both = not live; pauses while live and until `RESUME_AFTER_SECONDS` after the last time live was seen).
  `obs_streaming_from(cfg: dict) -> Callable[[], bool | None]` (`obsws_python.ReqClient(host=cfg.get("obs_ws_host", "localhost"), port=cfg.get("obs_ws_port", 4455), password=cfg.get("obs_ws_password", ""), timeout=3)`, `.get_stream_status().output_active`; any exception -> `None`).
  `backend_live_from(base_url: str, secret: str, http) -> Callable[[], bool | None]` (`GET {base}/api/uploader/live` with the secret, `timeout=5`; any failure -> `None`).

- [ ] **Step 1: Write the failing tests** (fake clock, fake callables):
  - `test_not_live_does_not_pause`, `test_obs_streaming_pauses`, `test_resumes_only_300s_after_stream_ends` (live at t=0, offline from t=30: paused at t=299, not at t=301), `test_obs_unreachable_falls_back_to_backend`, `test_neither_answers_means_upload`, `test_backend_not_asked_when_obs_answers`, `test_checks_at_most_every_30s`.
  - `test_obs_streaming_from_returns_none_on_error` (monkeypatch a fake `obsws_python` module in `sys.modules` whose `ReqClient` raises).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS.**

### Task 5: Gaming PC: job queue and worker (`uploader.py`)

**Files:**
- Create: `gaming pc/uploader.py`, `gaming pc/test_uploader.py`

**Interfaces:**
- Consumes: Task 3 (`query_offset`, `send_from`, `SessionExpired`), Task 4 (`LiveGate`, `obs_streaming_from`, `backend_live_from`), Task 2's routes.
- Produces:
  - `JOBS_DIR = Path(__file__).parent / "uploads"`; a job is `JOBS_DIR/<job_id>.json` = `{job_id, path, size, mtime, duration_s, stream_date, mime, project_id?, upload_url?}`, written atomically (tmp + `os.replace`).
  - `enqueue(paths: list[str], jobs_dir=JOBS_DIR, duration=media_duration) -> list[str]` returns user-facing messages for refused files: missing, size 0, or already queued (same path + size + mtime). `stream_date` = file mtime's local date; `mime` from `mimetypes.guess_type` or `video/mp4`; `duration` is computed here, once.
  - `media_duration(path) -> float | None` (`pymediainfo.MediaInfo.parse(path).general_tracks[0].duration` ms -> seconds; any failure -> `None`, which the backend treats as a VOD).
  - `try_lock(jobs_dir) -> file | None` (`.lock` file; `msvcrt.locking(LK_NBLCK)` on Windows, `fcntl.flock(LOCK_EX | LOCK_NB)` elsewhere; the OS drops it when the process dies, so a power cut cannot leave a stale lock), `unlock(fh)`.
  - `class Backend(base_url, secret, http)` with `register(job, new_session=False) -> dict` and `complete(project_id, file_id) -> dict`; raises `Refused(message)` on 401/400/409/507 (permanent: the job is dropped, except 401 which stops the worker), `requests.RequestException`/5xx propagate (temporary). Base URL = scheme + host of `agent_config.json`'s `backend_url`.
  - `run_job(job, backend, http, gate, progress, sleep=time.sleep, stop=lambda: False) -> str` returning `"done"`, `"dropped: <reason>"` or `"stopped"`. Steps: file still has the job's size and mtime, else drop ("<name> changed or moved since it was sent; send it again"); `register` until it has `project_id` + `upload_url` (save the job after); `status == "ready"` -> go straight to done; `query_offset`; on `SessionExpired` -> `register(new_session=True)`, save, start at 0; `send_from` with `on_chunk` that updates `progress(name, offset, size)` and waits (sleeping `RECHECK_SECONDS`) while `gate.should_pause()` and returns `False` when `stop()`; then `complete` until it succeeds; delete the job file. Temporary errors retry with `backoff(attempt) = min(5 * 2 ** attempt, 300)` seconds, forever (the PC or the Mac Mini may be rebooting).
  - `drain(jobs_dir, ...)`: loop `run_job` over jobs oldest-first; when empty, `unlock`, look again, and if a job appeared, `try_lock` again and continue (a sender that lost the lock race wrote its job before trying the lock, so either it wins the lock or this re-check sees its job).
- [ ] **Step 1: Write the failing tests** (fake `Backend`, fake `http` feeding Task 3's functions or monkeypatched `query_offset`/`send_from`, fake gate, no real sleeping):
  - `test_enqueue_writes_job_and_skips_duplicates`, `test_enqueue_refuses_missing_and_empty`, `test_enqueue_records_duration_and_local_date`.
  - `test_run_job_happy_path_completes_and_deletes_job_file`.
  - `test_run_job_resumes_from_server_offset` (job already has `upload_url`; `query_offset` -> 8 MiB; `send_from` called with offset 8 MiB; no new `register` session).
  - `test_run_job_restarts_on_session_expired` (`register(new_session=True)` called, upload from 0, job file holds the new URL).
  - `test_run_job_drops_when_file_changed` (mtime differs) and `..._when_file_missing`; the backend is never called.
  - `test_run_job_reuses_project_after_crash_between_register_and_save` (backend returns the same `project_id` for the same `job_id`: register twice, one project).
  - `test_run_job_retries_complete_until_backend_back` (two `ConnectionError`s then success; sleeps 5, 10).
  - `test_run_job_drops_on_refused` (507 message reaches the result), `test_run_job_ready_project_skips_upload`.
  - `test_run_job_waits_while_gate_paused` (gate pauses for two checks; `send_from`'s `on_chunk` sleeps twice before continuing).
  - `test_try_lock_is_exclusive` (second `try_lock` in the same test returns `None` while the first is held; works via `fcntl` on Linux).
  - `test_drain_picks_up_job_added_while_unlocking` (inject a hook that writes a job between `unlock` and the re-check; it gets uploaded).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Logging to `uploads/uploader.log` (rotating, 1 MB x 2) because `pythonw` has no console; never log the secret or the session URL (it authorises the upload).
- [ ] **Step 4: Run, expect PASS.**

### Task 6: Gaming PC: tray, sleep, install, ship

**Files:**
- Modify: `gaming pc/uploader.py` (`main(argv)`)
- Create: `gaming pc/install_uploader.py`, `gaming pc/test_install_uploader.py`, `gaming pc/UPLOADER_SETUP.md`
- Modify: `gaming pc/requirements.txt` (`pystray>=0.19.5`, `pymediainfo>=6.1.0`, `obsws-python>=1.7.0`, each with a one-line comment like the existing ones)

**Interfaces:**
- `uploader.main(argv)`: `uploader.py <file> [<file>...]` enqueues then works; `uploader.py --resume` (logon) only works the existing queue and exits at once when it is empty. Refusal messages and per-file results appear as tray notifications: on enqueue, one line per file with its duration and kind (`"clip_42.mp4: 0:42, clip"` / `"stream.mkv: 3:12:05, VOD"`; `duration_s < 300` = clip, unknown duration = VOD). If `try_lock` fails, exit 0 (the running instance takes the job). Otherwise start a `pystray.Icon` (a plain Pillow-drawn square; title `"Sending to editor: <name> 42% (1 of 3)"` / `"Paused while live"`; menu item "Stop (resumes at next login)"), run `drain` in the icon's setup thread with `ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` (`0x80000000 | 0x00000001`) set in that thread and cleared (`ES_CONTINUOUS`) in a `finally`; `icon.stop()` when done. 401 from the backend: notify "The agent secret is wrong - check agent_config.json" and stop.
- `install_uploader.py`: `command_for(pythonw: str, script: str, arg: str) -> str` (quoted: `"<pythonw>" "<script>" "<arg>"`), `pythonw_path() -> str` (`sys.executable` with `python.exe` -> `pythonw.exe`), `install()`, `uninstall()` via `winreg` under HKCU only (no admin): `Software\Classes\SystemFileAssociations\video\shell\DualBladeXSendToEditor` (default value `Send to editor`, `Icon` = pythonw path) with `\command` = `command_for(..., "%1")`; `Software\Microsoft\Windows\CurrentVersion\Run` value `DualBladeXUploader` = `"<pythonw>" "<uploader.py>" --resume` (no quoted arg). `python install_uploader.py [--uninstall]`.

- [ ] **Step 1: Write the failing tests:** `test_command_for_quotes_paths_with_spaces` (`C:\Program Files\Python314\pythonw.exe`, `C:\Users\aryan\credit agent\uploader.py`, `%1`), `test_pythonw_path_swaps_exe`, `test_main_resume_with_empty_queue_exits_without_tray` (no `pystray` import: assert `"pystray" not in sys.modules`), `test_main_exits_when_lock_held`, `test_enqueue_summary_line_formats_clip_and_vod`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, plus `UPLOADER_SETUP.md` in plain English for the streamer: `pip install -r requirements.txt`; in OBS: Tools -> WebSocket Server Settings -> enable, copy the password; add to `agent_config.json` `"obs_ws_password": "<that>"` (and `obs_ws_port` only if not 4455); `python install_uploader.py`; how to use (select files, right-click, Send to editor; on Windows 11 it is under "Show more options"; up to 15 files per right-click is Windows' limit); where the log is; `--uninstall`.
- [ ] **Step 4: Run all gaming PC tests (command in Global Constraints), expect PASS.** Then the backend suite once more.
- [ ] **Step 5: Ship.** Backend: branch `editor-app-plan-4` in `mac mini/Stream`, commit, push, PR to `aryanverma7/stream-backend`. Gaming PC: `gaming pc/` has no `.git`; clone `aryanverma7/pc-ocr` into `$CLAUDE_JOB_DIR/tmp/pc-ocr`, branch `editor-app-plan-4`, copy in only the new/changed files (`drive_upload.py`, `live_gate.py`, `uploader.py`, `install_uploader.py`, their tests, `requirements.txt`, `UPLOADER_SETUP.md`; never `agent_config.json`; first `diff` the clone's `requirements.txt` against the local one and stop if they differ beyond this plan's lines), add `uploads/` to its `.gitignore`, commit, push, PR. Push only with the user's go-ahead. Update the roadmap (tick rows, fill "Plan 4" in "What each plan hands on") and the `editor-app-project` memory.

## Not in this plan

- VOD reconcile on the backend: the uploader retries `complete` itself and keeps its job on disk until it succeeds.
- Persisting the byte offset: Google is asked for it on every resume (`query_offset`), which is the authoritative answer anyway.
- Stream title from Streamer.bot events: the Twitch poll's title is used when the poll saw the stream live that day; otherwise the filename.
- Frontend for any of it (Plan 5 shows projects, the VOD-ready popup and the Storage page).
