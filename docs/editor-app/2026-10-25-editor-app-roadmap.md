# Editor App — roadmap and coverage map

Spec: `docs/superpowers/specs/2026-10-24-editor-app-design.md`. The spec is the only source of
truth; if a decision changes, change the spec first, then this map.

**Rules**
- Every requirement below belongs to exactly one plan. Writing a plan = taking its rows from here.
- When a plan ships, tick its rows and fill in "What it hands on" with the exact names it created.
- Before writing plan N, re-read the spec, this file, and the "hands on" sections of plans < N.
  Anything unticked that belongs to an earlier plan is a gap: fix it before starting plan N.
- Moving a row to another plan is allowed; say so in its Notes column.

## Plans

| # | Plan | File | Status |
|---|------|------|--------|
| 1 | Foundation: security, roles, settings page | `2026-10-25-editor-app-1-foundation.md` | shipped 2026-10-26 (stream-backend#1, stream-site#1) |
| 2 | Editor backend: data, Drive, deletes, recovery, clips | `2026-10-26-editor-app-2-backend.md` | shipped (stream-backend#2 merged, deployed 2026-10-26) |
| 3 | Discord DMs and notifications | `2026-10-27-editor-app-3-discord.md` | Shipped 2026-10-26 (stream-backend#3) |
| 4 | Gaming PC uploader | `2026-10-27-editor-app-4-uploader.md` | PRs open 2026-10-28: stream-backend#4, pc-ocr#1 |
| 5A | Editor site: navigation, cards, uploads, checks, deletes, bell, Home, Fix list, Storage, CSP | `2026-10-28-editor-app-5a-site.md` | shipped 2026-10-28 (stream-backend#10, stream-site#4) |
| 5B | Review player and comments | `2026-10-29-editor-app-5b-player.md` | PRs open 2026-10-29: stream-backend#13, stream-site#7 (merge backend first) |

## Coverage map

### Security and settings (spec: "Also in this build", "Roles and login")
- [x] 1 · Role checks on every admin route; editor limited to `/editor`, `/api/editor`, `/auth/logout`
- [x] 1 · Secrets masked on GET /api/config; mask means "unchanged" on PUT
- [x] 1 · Secure cookie; sessions in SQLite, 30-day expiry, logout
- [x] 1 · `open_paths` matched by whole path segment
- [x] 1 · Atomic `config.json` writes
- [x] 1 · aiohttp 3.14.5, Pillow 13.x; python-socketio stays 4.6.1
- [x] 1 · next ≥16.4.1, npm audit fix. Node: the Mac Mini stays on 20.20.2 (Node 22+ needs macOS 11; Catalina is its ceiling). Accepted, since Node only builds and never serves
- [x] 1 · launchd plist checked against the Mac Mini copy (checked 2026-10-26: installed copy uses venv python)
- [x] 1 · ESLint flat config
- [x] 1 · Settings page: sections, search, typed inputs, validation, unsaved bar, masked secrets with Replace, Add setting, "Other" for unknown keys, Advanced raw JSON
- [x] 1 · Editor's GitHub login as config (`editor_github_username`)

### Data model and API (spec: "Data model", "Flows")
- [x] 2 · Projects (survive VOD deletion; date, title, filename; renamable)
- [x] 2 · Tapes (status Cutting / In review / Changes asked / Final; tape notes)
- [x] 2 · Iterations (Drive file id, size, check results, thumbnail, upload time)
- [x] 2 · Comments: point / range / general; type Fix / Keep / Question; threads; resolved state
- [x] 2 · Carry-over: unresolved comments become timestamp-less checkboxes on the next iteration, showing origin ("from v1 at 03:42")
- [x] 2 · Personal to-do list per login, private
- [x] 2 · Per-user read state (New badges, bell) and playback position (resume)
- [x] 2 · Search across comments and threads (API side)
- [x] 2 · Editor identity config: Google email, Discord user ID (alongside Plan 1's GitHub login)

### Google Drive (spec: "Flows", "Decisions made after the builder check")
- [x] 2 · One-time Drive connect (`drive.file`), refresh token saved atomically, app published to production
- [x] 2 · Folder layout per project/tape; brand kit folder created by the app and shared with the editor
- [x] 2 · Resumable upload sessions created server-side with the site's Origin (editor's browser gets only the session URI)
- [x] 2 · Share VOD with the editor's Google account; record the Download click
- [x] 2 · Drive free-space check API (used by uploader and iteration uploads)
- [x] 2 · Storage view API (app files, sizes, free space, scheduled deletes)

### Deletion and power cuts (spec: "Deletion rule", "Power cuts")
- [x] 2 · Two-step delete + 1-hour undo; permanent delete (not Trash); idempotent deleting→deleted; 404 = success
- [x] 2 · After any restart, pending deletes get at least a full hour again
- [x] 2 · Absolute-time scheduler; clock-sanity gate; retry with backoff, never record a failed attempt as done
- [x] 2 · Reconciliation of Drive folders vs. database (throttled scheduler job: first sane tick, then every 600 s while uploading)
- [x] 2 · Nightly SQLite backup to Drive
- [x] 2 · A bad `app_db_path` (missing folder, unwritable) falls back to the default path with a logged warning, so login and the Settings page still work and the setting can be fixed from the dashboard (final-review Minor 7 of Plan 1)
- [x] 2 · Final: mark final (confirm; the typed-name confirm is on the delete, the mark-final dialog is Plan 5) → download → two-step delete of all iterations; tape history kept
- [x] 1 · Mac Mini auto-restart + boot-time services: backend is a LaunchAgent with macOS automatic login (a LaunchDaemon cannot read ~/Documents under Catalina), cloudflared a LaunchDaemon, `pmset autorestart 1`. Reboot test passed 2026-10-27 (site reachable with no login). A real pull-the-plug test is still worth doing once

### Clips and reels (spec: "Clips and reels")
- [x] 2 · Project kind vod/clip from duration (< 300 s), auto single reel per clip, second reel refused
- [x] 2 · Combine 2+ clips into one reel (replaces their reels; a clip used once); uncombine while no versions
- [x] 2 · Start over: resolve all open comments on the latest version (admin)
- [x] 2 · Project delete refused for a clip hosting a combined reel until the reel's files are deleted
- [x] 4 · Uploader reports each file's duration; multi-file right-click makes one clip each
- [ ] 5 · Clips list oldest first, "used in" link, Combine/Uncombine UI, Start over button
- [ ] 5 · CSP + frame-ancestors + nosniff headers; recommend an extension-free browser profile

### Discord (spec: "Threads and notifications", "Flows")
- [x] 3 · Bot via REST (no gateway); token + both user IDs in config
- [x] 3 · VOD ready: one DM to the editor (+ popup on next visit, plan 5). Changed in plan 4 (2026-10-28): uploads finishing within 10 min of each other are one grouped DM
- [x] 3 · Delete-not-confirmed reminders: start 3h after Download click, hourly, 12:00–22:00 Mac Mini time, until confirmed
- [x] 3 · New iteration DM to the streamer
- [x] 3 · Comment/reply digests batched after ~10 min of quiet
- [x] 3 · One catch-up DM after an outage, never a burst; "site is back online" DM to the editor
- [x] 3 · In-site notification feed API (bell)

### Gaming PC uploader (spec: "Flow 1")
- [x] 4 · Explorer right-click "Send to editor" + logon task; windowless `pythonw`; tray progress
- [x] 4 · Registers the project with the backend (secret-gated, in `open_paths`)
- [x] 4 · Resumable upload with session URI + offset persisted; resume at logon; ~1 week session life (offset is asked from Google on resume instead of stored)
- [x] 4 · Pause while live (Mac Mini asks Twitch; OBS link dropped 2026-10-28), resume 5 min after; no answer = upload
- [x] 4 · Prevent sleep while uploading; free-space check before starting

### Editor frontend (spec: "Pages and navigation", "Flow 4", "Checks")
- [x] 5 · Zoom navigation + breadcrumb; Back/Esc zooms out (5A)
- [ ] 5 · Iteration cards: thumbnail, version, check badges, checklist summary card, hover/tap menu
- [x] 5 · Status labels with filter and sort (5A)
- [x] 5 · Editor Home, Fix list, Storage page, Transfers panel, To-do, Brand kit link (5A)
- [x] 5 · Iteration upload by drag and drop (resumable, pauses and resumes) (5A)
- [x] 5 · Review player: whole file into OPFS first, then playback; Chrome/Firefox on PC and laptop (5B)
- [x] 5 · Comments in the player: C to capture time, ranges, general comments, scrub markers, list follows playback, jump on click (5B)
- [x] 5 · Player keys: frame step, speed, `?` sheet; resume position; compare versions (low priority) (5B)
- [x] 5 · Unsent comments kept in browser storage and resent (5B)
- [ ] 5 · Checks: resolution, fps, bitrate (>= 50 Mbps); pass/warn/fail badges (no loudness/peak/length, streamer 2026-10-28)
- [x] 5 · Thread UI, New badges, bell, VOD-ready popup, search UI (5B)
- [ ] 5 · Visual design pass in the site's style (wireframes are layout only)

### Carried from Plan 2's final review (do first in Plan 3)
- [x] 3 · `complete_iteration_upload` refuses while a tape_files delete for that tape is pending/running (an abandoned iteration completed during the hour would escape the request)
- [x] 3 · deletions' clock-jump check: compare wall-clock delta against `time.monotonic()` delta so a slow job is not mistaken for a jump

### Later (not in any plan yet)
- Audit 2026-10-30 leftovers: forged Cloudbot replies still possible on YouTube (display names; needs a stable id or Streamer.bot bot flag); session tokens stored unhashed; the raw `drive.file` token handed to both roles can also delete files (proxy media or per-file links); brand kit not re-shared when `editor_google_email` changes, stale folder ids after reconnecting another account, `ensure_layout` not under the folder lock; project delete allowed while the VOD itself is uploading (unverified); public routes uncached (`/api/public/live-status`, `/videos`); `/api/status` waits on Spotify each poll; `_pending_logins` cap 32 can evict the streamer's login; `discord_live.stop_poller` never called; plist stdout log unrotated; `/api/logs` reads only the current rotated file; uploader drops a job on a Drive 403 rateLimitExceeded and after >5 failed job-file writes; `stream_date` uses mtime; unsent-queue entries from before the username change are never sent; 29.97 fps graded warn though the spec says under 30 fails (streamer to decide); checks/thumbnail missing when the tab closed during checking or reconcile completed the upload; a second tab can cancel another tab's upload
- 5B follow-ups (reviews, parked): V while playing auto-starts a partner that is not fully downloaded (streams; one-line change to require complete); `editor.resumePlaying` flag can linger if the partner never mounts; storage-only + video error Try again remounts instead of reloading; shift+reload leaves the page uncontrolled by the worker (shows "Reload"; could claim via a message); stale `media` message / timed-out need-link waiters / cancel race in media-sw.js; abort not checked in the download prelude; edit-in-place and composer draft polish; double GET /iterations in useComments; scrollIntoView pulls ancestors; SearchBox arrow keys and listbox semantics; StrictMode dev double-effect clears the composer flag; a Drive 403 rate limit reads as "no longer on Drive"; New badges ignore edits (created_at, spec says last-changed); test gaps listed in the 5B ledger
- 5A follow-ups (final review, parked): VodReadyPopup Esc test flaky (useLayoutEffect); download_project should record the click before minting a Drive token / return {ok}; cancel during "waiting for Mac Mini" orphans the finished file; Combine count can include a limbo clip; Home ignores project-delete limbo; native confirm/prompt dialogs to restyle in a design pass
- Verify in a real browser: Google exposes Range to CORS (Firefox), fps measuring, ProRes/HEVC thumbnails, Drive direct download for both accounts
- Restructure stream-backend into folders (streamer asked 2026-10-28): after PR #4 and #5 merge, before Plan 5
- Chat overlay blank after the gaming PC was off: Streamer.bot socket heartbeat, PR stream-backend#5
- Fix tests/test_credit_ocr.py (pre-existing, found 2026-10-26 on the Mac Mini): fixture images lack the "MIN NEXT ROUND" label the code now requires, and credits_5700_comma.png, credits_113_not_multiple_of_10.png, credits_no_label_120.png were never committed
- Mac Mini: backend is back to a LaunchAgent (a LaunchDaemon can't read ~/Documents under Catalina's privacy protection: PermissionError on venv/pyvenv.cfg); power-cut recovery relies on automatic login
- Export spec thresholds: supplied 2026-10-28 (spec "Checks on every iteration")
- Optional: editor side to Cloudflare; UPS

## What each plan hands on

### Plan 1
- `db.py`: stdlib sqlite3, `app.db` (WAL, `synchronous=FULL`); `db.get()`, `db.ensure(ddl)`, `db.use_path_for_tests(path)`. Connection is bound to the event-loop thread. One shared connection: never hold a transaction open across an await. Table: `sessions`.
- `auth.py`: `create_session`, `session_for`, `handle_logout` (`POST /auth/logout`), `role_for_username`, `path_matches`, `OPEN_PATHS`, `EDITOR_PATHS` (`/editor`, `/api/editor`, `/auth/logout`). Roles `admin`/`editor`; 30-day sessions; cookie Secure+HttpOnly+SameSite=Lax. Editors get 403 JSON on other APIs, 302 to `/editor` on other pages.
- Config keys: `authorized_github_usernames` (admin), `editor_github_username` (editor).
- `config.py`: atomic `save()` (tmp + fsync + `os.replace`).
- `config_schema.py`: `FIELDS` (must cover every non-underscore key in `config.example.json`), `SECRET_MASK` (`"••••••••"`), `masked()`, `schema_for()` (unknown keys go to "Other"), `validate_update()`. Sections include "Break screen", "Login & site", "Editor app", "Other".
- `dashboard_api.py`: `GET /api/config` (masked), `GET /api/config/schema`, `PUT /api/config` (validates all, writes nothing on error, mask means unchanged, merges `_custom_fields`).
- Site: `lib/settingsModel.ts`, `lib/useConfigEditor.ts` (schema + `schemaMissing` fallback), `components/admin/ConfigEditor.tsx` + `SettingsField.tsx` (Settings page), `eslint.config.mjs`, next 16.4.3.
- Deps: aiohttp 3.14.5, Pillow 13.0.0 (Mac Mini; gaming PC `Pillow>=11.0.0`), aioresponses 0.7.9 (dev, shimmed in `conftest.py`).
- Tests run in WSL via uv (see CLAUDE.md); `tsconfig.tsbuildinfo` is tracked and dirtied by tsc.
- Known gap: `README.md` in stream-backend still describes the LaunchAgent install; the real setup is LaunchDaemons (backend and cloudflared).

### Plan 2
- Docs: `GDRIVE_SETUP.md` (streamer's one-time Google setup + "If the database is ever corrupted"); CLAUDE.md subsection "The editor backend keeps its state in `app.db`...".
- `db.py` additions: `transaction()` (BEGIN IMMEDIATE; the only multi-statement write path, `with conn:` is a no-op), `meta_get`/`meta_set` (table `meta`). Corrupt `app.db` raises; fallback to `DEFAULT_PATH` only when the configured path cannot be opened.
- `gdrive.py`: `access_token`, `token_expires_at`, `about`, `create_folder`, `start_resumable_upload(name, parent_id, size, mime, app_properties, origin)`, `get_file`, `list_folder`, `share_with`, `delete_file` (permanent, 404 ok), `upload_small_file`; `DriveNotConnected`, `DriveError`. Constants `SCOPE`, `AUTH_URL`, `TOKEN_URL`, `API`, `UPLOAD_API`.
- `gdrive_oauth.py`: `ensure_layout()`; routes `GET /auth/gdrive/login`, `GET /auth/gdrive/callback` (admin only). `meta` keys `gdrive_root`, `gdrive_projects`, `gdrive_brand_kit`, `gdrive_backups`, `gdrive_brand_kit_shared`, `backup_last_date`, `backup_attempts`, `backup_next_try`, `reconcile_last_run`, `clock_last_seen`.
- `editor_store.py`: tables `projects`, `tapes`, `iterations`, `events`; `create_project`, `get_project`, `list_projects`, `rename_project`, `set_project_folder`, `set_vod_upload`, `mark_vod_ready`, `record_download_click`, tape and iteration CRUD (`create_tape`, `update_tape`, `create_iteration`, `complete_iteration` (also abandoned -> ready; records `iteration_ready` in the same transaction), `abandon_iteration`, `any_uploading`, `set_iteration_media`, `previous_ready_iteration`), `record_event`, `events_since(after_id, exclude_actor=None)`.
- Event kinds: `tape_created`, `vod_ready`, `iteration_ready`, `comment_added`, `reply_added`, `comment_resolved`, `start_over`, `reels_combined`, `reels_uncombined`, `delete_scheduled`, `delete_undone`, `delete_done`.
- Clips and reels (Task 13): `projects.kind` (`vod`/`clip`), `projects.duration_s`, `tapes.kind` (`tape`/`reel`), table `reel_sources(tape_id, project_id UNIQUE)`. `editor_store.CLIP_MAX_SECONDS` 300; `create_project(..., duration_s=None)` (clip when `< 300`, its reel created in the same transaction), `reel_for_clip`, `insert_reel`; `get_project` carries `reel_tape_id`, `get_tape`/`list_tapes` carry `source_project_ids` (oldest clip first); `create_tape` on a clip raises "A clip has exactly one reel". `editor_service.combine_clips(project_ids, name, actor)` and `uncombine(tape_id, actor)` (both only while no involved reel has any iteration row; combining must list every clip already on an involved reel). `editor_comments.start_over(iteration_id, by)`. `deletions` refuses a project delete for a clip hosting a combined reel until that reel's `files_deleted_at` is set. Routes `POST clips/combine`, `POST tapes/{id}/uncombine` (both roles), `POST iterations/{id}/start-over` (admin).
- `editor_comments.py` (tables `comments`, `replies`, `comment_state`; `add_comment`, `add_reply`, `set_resolved`, `carry_over`, `search`), `editor_personal.py` (tables `todos`, `tape_seen`, `playback`).
- `editor_service.py`: `free_space`, `ensure_project_folder`, `ensure_tape_folder`, `create_tape`, `start_iteration_upload` (raises `DriveFull`), `complete_iteration_upload`, `vod_ready`, `media_link` (token only in a JSON body, a `drive.file` token with up to 1 hour left, at least 10 min), `storage_view`. Tape/upload/`vod_ready` calls refuse while a project delete is live.
- `scheduler.py`: `register(name, async job(now))`, `start`/`stop`, `tick`, `status()` (`sane`, `last_seen`, `paused_since`), `reset_clock()`, `boot_time()`, `backoff_seconds`, `TICK_SECONDS` 30. Clock gate pauses every job while the clock is behind `clock_last_seen`.
- `deletions.py`: tables `delete_requests`, `delete_files`; `request_delete`, `undo`, `pending`, `apply_boot_grace`, `run_due` (applies the boot grace on its first sane run and after a >600 s forward clock jump), `clamp_after_clock_reset` (called by the reset-clock route), `ensure_project_not_deleting`, `NotFound`, `DELETE_GRACE_SECONDS` 3600. Project deletes refuse while uploading and never overlap a live vod/tape_files delete. An editor may delete a VOD only after his own Download click (`projects.download_clicked_at`).
- `reconcile.py` `run_once` + scheduler job `run_if_due` (first sane tick after boot, then only while an iteration is uploading, at most every 600 s); `backup.py` `run_if_due` (04:00 local, keep 7; idle without a Drive token, backoff after failure). All registered in `main.py`.
- Routes under `/api/editor`: `me`, `projects` (+ `/{id}`, `/download`, `/tapes`), `tapes/{id}` (+ `/iterations`), `iterations/{id}` (+ `/complete`, `/thumbnail`, `/media`, `/media-link`, `/position`, `/comments`), `comments/{id}` (+ `/replies`), `iterations/{iid}/comments/{cid}/resolved`, `search`, `todos`, `deletions` (+ `/{id}/undo`), `storage` (admin; includes `scheduler`), `scheduler/reset-clock` (admin), `drive/status` (admin). Admin checks are inside the handlers.
- Config keys: `gdrive_client_id`, `gdrive_client_secret`, `gdrive_redirect_uri`, `gdrive_refresh_token`, `editor_google_email`, `editor_discord_user_id` (all "Editor app").
- For Plan 3: `events`/`events_since`, the event kinds above, `projects.download_clicked_at`, `delete_requests`, `scheduler.register`, `editor_discord_user_id`.
- For Plan 4: `editor_store.create_project` (pass `duration_s` so clips are classified), `editor_service.ensure_project_folder`, `editor_store.set_vod_upload`, `editor_service.vod_ready`, `editor_service.free_space`, `gdrive.start_resumable_upload`. No project-creation route exists yet: Plan 4 adds it (secret-gated, in `OPEN_PATHS`). **VOD uploads are NOT reconciled**: if `vod_ready` never arrives the project stays `uploading` forever, so Plan 4 must retry `vod_ready` itself (or add VOD reconcile with an `appProperties` convention, e.g. `{"project_id": "<id>"}`, matched in `reconcile.run_once`).
- Not built here: any frontend (Plan 5); Discord (Plan 3).

### Plan 3
- Docs: `DISCORD_BOT_SETUP.md` (streamer's one-time setup); CLAUDE.md subsection "Editor-app Discord DMs...".
- Modules: `discord_dm.py` (`send_dm`, `DiscordError`, `DiscordNotConfigured`), `notify_outbox.py` (`enqueue`, `deliver_due`, `reset_stuck_sending`, `ensure_schema`, `status`; table `dm_outbox`), `notifier.py` (`run`, `back_online_once`, `label(tape_id, iteration_id)`, table `digest_pending`), `reminders.py` (`run`), `editor_bell.py` (`feed`, `mark_seen`). Registered in `main.py` as scheduler jobs `notifier`, `reminders`, `dm_outbox`, `back_online`.
- Routes (both roles): `GET /api/editor/notifications?after=<event id>` -> `{items, unseen}` (other people's events only, comment text replaced by a label); `POST /api/editor/notifications/seen {up_to_id}` (clamped to the newest event). `GET /api/editor/storage` now has a `"discord"` block (`notify_outbox.status()`: `enabled`, `pending`, `sending`, `dropped`, `last_error`, `last_sent_at`).
- Config keys ("Editor app"): `discord_bot_token` (secret), `streamer_discord_user_id`, `editor_discord_user_id`, `discord_notifications_enabled` (default false).
- Event kinds that DM: `vod_ready` (editor), `iteration_ready` (streamer; actor `system` or editor), `comment_added`/`reply_added`/`comment_resolved`/`start_over` (digest to the other person after 600 s quiet). Plus reminders (editor) and back-online (editor, outage > 600 s).
- Plan 2 carry-overs fixed: monotonic jump check in `deletions`; `complete_iteration_upload` refuses during a `tape_files` delete.
- `db.ensure` raises inside a transaction: ensure schemas before `db.transaction()`.
- For Plan 5: the bell's `after=` plus a 50-item newest-first page can skip events if more than 50 arrive between polls; page, or poll without `after` when the panel opens.
- For Plan 5: the VOD-ready popup on next visit reads the bell (`GET /api/editor/notifications`) for `vod_ready` items; bell `unseen` drives the badge; call `/notifications/seen` when the panel opens.
- For Plan 4: the uploader's `vod_ready` actor must be `"agent"` (never the editor's username), or the editor would be notified of his own action (the bell and digests skip the actor's own events).
- Not built here: any frontend (Plan 5).

### Plan 5B
- Backend: `comments.client_id` / `replies.client_id` (partial UNIQUE index, migrated lazily by `editor_comments._cc()`); `add_comment`/`add_reply(..., client_id)` return the existing row on a repeat (route 201 then 200; `^[A-Za-z0-9_-]{8,64}$`); `ec.by_client_id`. Table `iteration_seen`, `ep.mark_iteration_seen`, route `POST /api/editor/iterations/{id}/seen` -> `{previous_seen_at}`. `PAGE_CSP` connect-src adds `https://*.googleusercontent.com`; `Service-Worker-Allowed: /editor` on exactly `/editor/media-sw.js`.
- Site download: `lib/opfsMedia.ts` (`CHUNK_BYTES` 32 MiB, `dirName` `it-<p>-<i>-<fileId>`, chunk files `c00000`.., `presentChunks` exact-size rule, `downloadToOpfs`, `keepOnlyProject` (EditorShell, every route change), `freeSpaceFor`, `FatalMediaError`/`MediaGone`/`NotEnoughStorage`), `lib/useMediaFile.ts` (`getLink` stamps `expires_at`).
- Site playback: `public/editor/media-sw.js` + `public/editor/media-range.js` (`parseRange`, `planRange`, `linkFresh`; constants pinned against opfsMedia by a test), `lib/useMediaWorker.ts` (`mediaUrl`, `provide`, need-link), `components/editor/DownloadRing.tsx`.
- Site player: `ReviewPlayer.tsx`, `ScrubBar.tsx`, `KeySheet.tsx`, `lib/usePlayerKeys.ts`, `lib/timecode.ts`, `lib/isTyping.ts`.
- Site comments: `lib/unsentQueue.ts` (`editor.unsent`), `lib/useComments.ts`, `CommentPanel.tsx`, `CommentComposer.tsx`, `CommentItem.tsx`.
- Site compare/search: V partner logic in ReviewPlayer, `SearchBox.tsx`, `lib/useSearch.ts`, card "Add comment" (`sessionStorage editor.openComposer`).
- Not verified in a real browser: service-worker Range handling in Firefox/Chrome, Drive `alt=media` redirect behaviour, real 1–4 GB files (smoke list in the site PR).

### Plan 4
- Backend: `uploader_api.py` (`POST /api/uploader/projects` idempotent by `job_id` via `meta uploader_job_<job_id>`, `new_session`; `POST /api/uploader/projects/{id}/complete` -> `vod_ready(actor="agent")`; `GET /api/uploader/live`, asks Twitch directly, cached 60 s), all behind `X-Agent-Secret` == `ocr_agent_secret`, `/api/uploader` in `OPEN_PATHS`. `editor_api.map_errors`. `discord_live.live_state()`, `last_live_title(date)`. `vod_ready` now verifies parents, `appProperties.project_id` and size.
- Gaming PC (`pc-ocr` repo = `gaming pc/`): `drive_upload.py`, `live_gate.py`, `uploader.py` (queue in `uploads/*.json`, lock hand-over, notices in `uploads/notices/`, tray, keep-awake, log `uploads/uploader.log`), `install_uploader.py` (HKCU verb + Run key), `UPLOADER_SETUP.md`. No new PC config keys (reuses `backend_url`, `agent_secret`).
- For Plan 5: a dropped PC upload (file changed, refused after register) leaves a project stuck `uploading`; the project list should show these so the streamer can delete them.
- Known small gaps: `create_project` + meta row not one transaction (microsecond window); a permanent Google 4xx retries forever with no tray hint.

