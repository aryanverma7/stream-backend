# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

DualBladeX stream automation: a Valorant streamer's custom stack spanning two physical machines and three independent codebases that talk to each other over HTTP/WebSocket. There is no shared build, no monorepo tooling, and no git repository — each directory is its own project.

```
mac mini/Stream/   Python aiohttp backend (deployed to ~/Documents/Stream on a 2012 Mac Mini);
                   modules in core/, stream/, editor/ (2026-10-28), docs/ setup guides, deploy/ plist
mac mini/Site/     Next.js 16 static-export frontend (deployed to ~/Documents/Site)
gaming pc/         Windows Python screen-capture agent (deployed to credit-agent/)
widgets/           OBS browser-source widgets, a clone of aryanverma7/stream-widgets (one folder per widget, no build step)
```

The two top-level design documents are `project foundation.md` (the original task-by-task
plan for the frontend hero/shader/gateway work) and `project state.md` (what exists now).
`project state.md` references a `PROJECT_NOTES.md` that is **not present in this tree**, and
`project foundation.md` references a spec at `docs/superpowers/specs/2026-08-19-showcase-homepage-design.md`
that is also absent. `project foundation.md` is byte-identical to
`mac mini/Site/docs/2026-08-20-showcase-frontend-foundation.md` — edit both or neither.

The two docs use **different, colliding task numbers**: `project state.md`'s "Task #7" is OBS
auto file-splitting (from the missing `PROJECT_NOTES.md` numbering), while `project foundation.md`'s
"Task 7" is the `HiddenGateway` component. Always say which document a task number comes from.

## Commands

### Backend (`mac mini/Stream`)

```bash
python3 main.py                          # run the whole backend (single asyncio process)
curl http://localhost:8765/health        # confirm it's alive
python3 -m pytest tests/                 # full suite (on the Mac Mini)
# from WSL (pip is absent; uv supplies the deps, no Mac Mini needed):
uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings
python3 -m pytest tests/test_roulette.py::TestVote -k "cost"   # single test
```

Deps: `requirements.txt` (runtime) + `requirements-dev.txt` (pytest, pytest-asyncio, aioresponses).
Runs under launchd in production via `com.dualbladex.backend.plist` as a **LaunchAgent**
(`~/Library/LaunchAgents/`), so it starts only after login; power-cut recovery therefore relies on
macOS **automatic login** plus `pmset autorestart 1`. **Do not move it to a LaunchDaemon**: tried on
2026-10-26, and Catalina's privacy protection (TCC) blocks a boot-time service from reading
`~/Documents` - the backend died with `PermissionError: Operation not permitted: .../venv/pyvenv.cfg`.
(The alternative would be moving Stream and Site out of `~/Documents`.) `cloudflared` is a
LaunchDaemon and is fine, since it never touches `~/Documents`. Crash logs:
`/tmp/mac_mini_backend.{out,err}.log`. The Mac Mini's venv needs `requirements-dev.txt` installed to
run the tests (`venv/bin/pip install -r requirements-dev.txt`).

### Frontend (`mac mini/Site`)

```bash
npm run build                            # static export -> out/
npm run dev
npx vitest run                           # no "test" script exists in package.json
npx vitest run components/admin/ConfigEditor.test.tsx   # single file
npx vitest run -t "refuses to save"      # single test by name
```

25 test files / 193 tests, all passing; `npm run lint` works (flat `eslint.config.mjs`, 0 errors, a few warnings) and `npx next build` produces `/`, `/_not-found`, `/admin`.
The very first cold run after a fresh `npm install` can spuriously fail one `ConfigEditor` test
(the suite reports a ~16s environment setup on that run); re-run before investigating.

### Agent (`gaming pc`)

```bash
python calibrate.py                      # screenshot-then-drag region picker; must run before agent.py
python agent.py                          # keyboard hook: press B to fire a capture burst
python -m pytest                         # tests live beside the source, not in tests/
```


## Architecture

### Backend is one asyncio process, wired in `main.py`

Since 2026-10-28 (stream-backend#9) the modules live in packages: `core/` (config, config_schema, db,
auth, logger, server, site_server, dashboard_api, public_api, health_checks, scheduler), `stream/`
(everything stream-facing) and `editor/` (the editor app). Module names below are unchanged; import
them as `from core import db`, `from editor import editor_store as es`. Runtime files (`config.json`,
`app.db`, `logs/`, `clips/`, state JSON) stay in the repo root; modules resolve them via
`Path(__file__).parent.parent`.

`main.py` is deliberately thin: it starts `run_server()` (the aiohttp app), opens the outbound
Streamer.bot connection, registers chat handlers, and optionally starts the Streamlabs tips
listener. All feature logic lives in its own module. `server.py` registers `/health`,
`/ws/widgets`, the auth routes, and the OCR routes; `dashboard_api.py` and `public_api.py`
register their own routes onto the same app; `site_server.py` registers **last** because its
catch-all `/{path:.*}` route would otherwise shadow everything else. That catch-all matches a
full path, not one segment, and this matters: Next 16's client-side router fetches a route's
data from nested files (`/admin/__next._tree.txt`, `/admin/__next.admin.__PAGE__.txt`), so the
earlier single-segment `/{filename}` pattern 404'd them however present they were in `out/` and
every client-side navigation to `/admin` died with "This page couldn't load" until the page was
reloaded by hand. `_resolve_within()` tries the exact path, then `<path>.html`, then
`<path>/index.html`, and refuses anything resolving outside `out/`.

### `auth.py`'s `OPEN_PATHS` allowlist runs before every handler

The auth middleware short-circuits on a match against `OPEN_PATHS` and 401s otherwise.
Matching is by **whole path segment** (`path_matches`), not string prefix, so `/clips` no
longer opens `/clipsadmin`. Any new endpoint intended for a non-browser client (anything with its
own shared-secret or token check) **must be added to `OPEN_PATHS`**, or its own auth code never
executes. This is why `/api/ocr/credit-report`, `/api/ocr/reset`, `/api/ocr/heartbeat` and
`/api/uploader` are listed there despite being secret-gated (the OCR ones share
`credit_ocr._agent_secret_ok()`, the uploader checks the same `ocr_agent_secret` — one machine,
one trust boundary, one secret).
Adding a route without touching this list is the most common way to break things here.

**There are two roles.** `role_for_username()` maps a GitHub login to `admin`
(`authorized_github_usernames`) or `editor` (`editor_github_username`); anyone else is refused at
the callback. `EDITOR_PATHS` (`/editor`, `/api/editor`, `/auth/logout`) is all an editor may
reach: other APIs answer them a 403 JSON, other pages a 302 to `/editor`, and the callback sends
an editor's `next` somewhere outside that list to `/editor` too. Admins reach everything.

**Sessions live in the `sessions` table of `app.db`** (`db.py`: stdlib `sqlite3`, WAL,
`synchronous=FULL`, one connection bound to the event-loop thread; `db.ensure(ddl)` creates
tables, `db.use_path_for_tests()` redirects it). `create_session`/`session_for` give a 30-day
expiry, `POST /auth/logout` deletes the row, and the cookie is `Secure`+`HttpOnly`+`SameSite=Lax`.
Consequence: **login over the plain-HTTP LAN address no longer works** - use the HTTPS hostname.
Sessions survive restarts (they used to be in memory). **`app.db`, `app.db-wal` and `app.db-shm`
must never be committed** - they hold live sessions, and `config.json` stays ignored likewise.
`config.save()` is atomic (tmp file + fsync + `os.replace`), so a crash mid-save cannot truncate it.

### `config_schema.py` is the one source of config metadata, and `/api/config` is masked

`FIELDS` gives every config key its section, label, type and help text (sections include
"Break screen", "Login & site", "Editor app" and a catch-all "Other"); `schema_for()` serves it
at `GET /api/config/schema` and files any key it has no entry for under "Other". **A new key in
`config.example.json` needs a `FIELDS` entry** or `test_every_example_key_has_metadata` fails.
`GET /api/config` returns `masked()` data: secret values (by `FIELDS` flag or by name) become
`SECRET_MASK` (`"••••••••"`, eight U+2022 - identical string in `lib/settingsModel.ts`).
`PUT` runs `validate_update()` over **all** fields first and writes **nothing** on any error,
treats a value equal to the mask as "unchanged", and merges `_custom_fields`. The Settings page
is `components/admin/ConfigEditor.tsx` + `SettingsField.tsx` over `lib/useConfigEditor.ts`
(`schemaMissing` falls back to the raw-JSON view when the backend is older than the site).
A `FIELDS` extra `"needed"` (`True`, or a bool key that must be on) makes the page's red banner
(`missingFields()`) flag the key while empty; `with_needed()` serves absent needed keys as empty so
they can be filled from the page.

### The editor backend keeps its state in `app.db` and its files on Google Drive

The editor app (a second GitHub login, role `editor`) is the data, Drive and deletion layer under
`/api/editor`. There is no frontend for it yet (Plan 5). Modules, each one job:

- `gdrive.py` the Drive client (`drive.file` scope, aiohttp only); `gdrive_oauth.py` the admin-only
  one-time connect (`/auth/gdrive/login`, `/auth/gdrive/callback`, in neither `OPEN_PATHS` nor
  `EDITOR_PATHS`) and `ensure_layout()`. Streamer-facing setup is `GDRIVE_SETUP.md`.
- `editor_store.py` projects, tapes, iterations and the `events` log; `editor_comments.py` comments,
  replies, resolved state, carry-over and search; `editor_personal.py` private to-dos, per-user
  "new" state and playback position (every read is keyed by username, so another user's row is
  simply not found).
- `editor_service.py` the flows that touch Drive (folders, upload sessions, `vod_ready`,
  `media_link`, `storage_view`); `editor_api.py` the routes, which only translate.
- `scheduler.py` one 30s loop for every timed job; `deletions.py` two-step deletes;
  `reconcile.py` upload recovery (a throttled scheduler job); `backup.py` the nightly DB copy.

**Large files never pass through the Mac Mini.** The backend opens a resumable-upload session with
the site's `Origin` and hands the editor's browser only the session URI; bytes go browser to Google.
Playback and download read from Drive directly. The single exception is `upload_small_file`, for
the few-MB database backup. A browser that finishes an upload while the backend is off (or whose
`/complete` never arrives) is caught by `reconcile.run_once()`, which runs as the scheduler job
`reconcile`: on the first sane tick after boot regardless, then only while some iteration is
`uploading`, at most every 600 s (`meta.reconcile_last_run`). It matches Drive files to iterations
by `appProperties.iteration_id`, completes them, and marks an iteration still `uploading` after 8
days `abandoned` (guarded `WHERE status='uploading'`); an abandoned iteration goes back to `ready`
only through `/complete` (the browser's call, e.g. after a session renewal) - reconcile itself scans `uploading` rows only. A `DriveError` on one iteration does not end the pass;
`DriveNotConnected` ends it quietly. Unknown files are left alone, never deleted. **VOD uploads are
not reconciled** - only iterations carry `appProperties`.

**Plan 5A site routes** (both roles): `GET /api/editor/home` (`vods_ready`, `deletes_to_confirm`,
`open_tapes`), `GET /api/editor/fix-list` (unresolved comments on each tape's latest ready version, oldest
first; both skip projects in project-delete limbo, both reuse `editor_api._open_comments`), and
`POST /api/editor/iterations/{id}/session` (fresh resumable session for a version still `uploading`, same
`svc._upload_session` as the upload start; an `abandoned` one goes back to `uploading`; 409 otherwise). `/me` adds `brand_kit_url`. Security headers are
`core/server.py`'s `on_response_prepare` hook, not a middleware (a FileResponse's content type is only known
there): nosniff + `frame-ancestors 'none'` everywhere, `PAGE_CSP` on every HTML response except `/` and `/index.html`.

**Every time is an absolute timestamp in SQLite, behind the scheduler's clock gate.** The Mac Mini
loses power and its clock battery may be weak, so it can boot with the clock in the past. `tick()`
refuses to run any job while `now` is behind `meta.clock_last_seen`, and records the high-water
mark with `max()` after a sane tick. `GET /api/editor/storage` (admin) shows
`scheduler.paused_since`; `POST /api/editor/scheduler/reset-clock` (admin) trusts the current clock
again, which is the way out when the clock once jumped *forward* and now looks permanently behind.
That button is now only the fallback: while paused, at most every 300 s (monotonic), `tick()` asks
`core/net_time.online_time()` (median `Date` header of google.com/cloudflare.com, HEAD then GET, 5 s)
and, if it is within 120 s of the local clock, resets the gate itself and runs the `on_auto_reset`
callbacks (`main.py` registers `deletions.clamp_after_clock_reset`). Disagreement stays paused, logged
once per pause; `status()` reports `last_internet_check {at, agreed}` (`agreed: null` = no answer).
Timing code takes `now: float | None = None`, as everywhere else.

**Deletes are two-step, permanent, idempotent, and get the boot grace.** Nothing is deleted without
typing the exact name; `request_delete` then waits `DELETE_GRACE_SECONDS` (3600) with an undo.
`DELETE /files/{id}` is permanent (never Trash) and a `404` is success, so a retry after a power
cut is harmless. Each Drive call is bracketed by its own short transactions (mark `deleting`,
commit, await Drive, record the outcome), because **a transaction is never held across an `await`**.
Failures retry with `min(30 * 2**attempts, 3600)` seconds of backoff and are never recorded as done.
**Boot grace is applied on the first sane `run_due`, not at process start**: a start-time grace
could be computed from a 2001 clock, so every pending delete gets a full hour again from the first
trustworthy time (`main.py` deliberately does not call `apply_boot_grace`), and again whenever
`now` moved more than 600 s since the previous `run_due` (a forward clock jump; extra grace is the
safe direction). `reset-clock` (and the automatic internet reset) sets every pending `delete_at` to an hour from the
corrected now when it was further out *or already past* (a past-due delete must not fire the moment the gate reopens) (`deletions.clamp_after_clock_reset`, called from the route so `scheduler` never
imports `deletions`). A project delete is refused while any of its iterations is `uploading` and
never overlaps a live `vod`/`tape_files` delete in that project; while one is live, new tapes,
upload starts/completions and `vod_ready` refuse ("This project is being deleted"). Targets are `vod`,
`tape_files` and `project`; a target is marked deleted only through its own `delete_files` row.
**An editor's Download click is what lets him delete the VOD** (`projects.download_clicked_at`,
set once, only for role `editor`); the streamer's click does not count, or he could not be the one
to confirm having the file. Admin-only checks happen **inside the `/api/editor` handlers** (the
role is checked there, answering `403 {"error": "Only the streamer can do this."}`) because
`EDITOR_PATHS` lets both roles reach the whole prefix.

**The review player (Plan 5B)** downloads a version into the browser's OPFS as 32 MiB chunk files
(`it-<projectId>-<iterationId>-<driveFileId>/c00000`, present only at exact size), straight from
`www.googleapis.com/drive/v3/files/<id>?alt=media` with the `media-link` token in an `Authorization` header
(never a URL or storage). Playback always goes through the service worker `public/editor/media-sw.js`
(scope `/editor`, allowed by the backend's `Service-Worker-Allowed` header): `<video src="/editor/media/<id>">`
is answered from chunks on disk, or from Drive for missing ranges ("Start watching now"). Versions are kept
while the route stays inside their project and deleted on leaving it (`keepOnlyProject`, the streamer's rule).
Comment and reply posts carry a `client_id` (idempotent; the unsent queue in `localStorage editor.unsent`
resends with the same id), and `POST /iterations/{id}/seen` is what New badges compare against.

**`POST /api/editor/iterations/{id}/abandon`** (both roles) is the site's Cancel: `uploading` ->
`abandoned` (409 otherwise), so a cancelled upload stops blocking `tape_files`/project deletes for 8 days;
renewing a session (`/session`) sets `updated_at`, and reconcile's 8-day abandon counts from
`max(created_at, updated_at)`.
**Every completed iteration is shared with `editor_google_email` as reader** (`complete_iteration_upload`,
which reconcile also goes through), like `vod_ready` does; a failed share is a warning, never a failed completion.

**A clip is a project with exactly one reel, and `reel_sources` is what a reel is made of.** An
upload under `editor_store.CLIP_MAX_SECONDS` (300) is `projects.kind='clip'`, decided only in
`create_project(duration_s=...)`, which creates its reel (`tapes.kind='reel'`) in the same
transaction; `create_tape` on a clip refuses. A reel is otherwise a tape (versions, comments,
carry-over, Final, deletes). `combine_clips` keeps the first clip's reel, renames it, repoints every
listed clip's `reel_sources` row at it and deletes the other reels; `uncombine` gives each clip its
own reel back (the hosting clip keeps the original). Both refuse once any involved reel has an
iteration row of any status, and combining must list every clip already on an involved reel, so
`reel_sources.project_id UNIQUE` (a clip in at most one reel) never has to be the error a user sees.
The reel lives in the first clip's project, which therefore cannot be project-deleted until that
reel's files are deleted. `editor_comments.start_over` (admin, `POST iterations/{id}/start-over`)
resolves every open comment on that one iteration; earlier versions keep their state.

**`db.transaction()` (`BEGIN IMMEDIATE`) is the only way to do a multi-statement write.** The
connection is autocommit (`isolation_level=None`), so `with conn:` does nothing. `db.ensure(ddl)`
`executescript`s, which COMMITs - run every DDL *before* opening a transaction. Tables are created
lazily by their module (`editor_store`, `editor_comments`, `editor_personal`, `deletions`), never
at import.

**A corrupt `app.db` fails loudly; there is no silent fallback.** `db.get()` falls back to the
default path only when the configured `app_db_path` cannot be *opened* (missing folder, unwritable,
not a path), logging a warning, so login and Settings still work and the path can be fixed from the
dashboard. Corruption or a lock raises, because a silently swapped-in empty database would log
everyone out and hide the data. Recovery: stop the backend, download the newest `app-YYYY-MM-DD.db`
from Drive's "DualBladeX Editor/Backups", put it at the db path (remove stale `-wal`/`-shm`),
start again. `backup.run_if_due` runs daily after `BACKUP_HOUR` (4, local time) via
`sqlite3.backup()` and keeps the newest `BACKUP_KEEP` (7); it does nothing until
`gdrive_refresh_token` is set, and after a failure waits `backoff_seconds(meta.backup_attempts)`
(`meta.backup_next_try`). The backend still starts on a corrupt `app.db`; logins and editor calls
fail with a database error. The streamer-facing version of this is in `GDRIVE_SETUP.md`.

**The `media-link` token lives only in a JSON body, for an `Authorization` header, never in a URL**
(URLs reach logs, history and `Referer`). It is a `drive.file` access token with up to 1 hour left
(refreshed once fewer than 600 s remain, so a link gets at least ten minutes; `expires_in` is
reported). That ceiling is real: a raw `drive.file` token reaches *every* file this
app created, not just the requested one. Both users can already open them all, so it is accepted
(the `ponytail:` comment in `media_link`); proxy or mint per-file links if a third user ever joins.

**Drive constants**, all in `gdrive.py`: `SCOPE` `.../auth/drive.file`, `AUTH_URL`
`accounts.google.com/o/oauth2/v2/auth` (sent with `access_type=offline` **and** `prompt=consent`,
or a repeat connect returns no refresh token), `TOKEN_URL` `oauth2.googleapis.com/token`, `API`
`.../drive/v3`, `UPLOAD_API` `.../upload/drive/v3/files`. Folders: `DualBladeX Editor` holding
`Projects`, `Brand kit` (shared read-only with `editor_google_email`) and `Backups`; a project
folder is `"{stream_date} {name}"`, each tape its own subfolder; an iteration file is
`"v{version} - {filename}"` with `appProperties {"iteration_id": "<id>"}`. Folder ids live in the
`meta` table (`gdrive_root`, `gdrive_projects`, `gdrive_brand_kit`, `gdrive_backups`). The refresh
token is rotated and saved like Spotify's. Limits: thumbnail 262144 bytes, checks JSON 16384,
search 50 hits.

**What later plans build on.** Plan 3 (Discord) reads `editor_store.events` / `events_since(after_id,
exclude_actor)`; kinds in use are `vod_ready`, `iteration_ready`, `comment_added`, `reply_added`,
`comment_resolved`, `delete_scheduled`, `delete_undone`, `delete_done`. It also needs
`projects.download_clicked_at` and the `delete_requests` table for its reminders,
`scheduler.register(name, async job(now))` for timed work, and the config key
`editor_discord_user_id`. Plan 4 (gaming-PC uploader) needs `editor_store.create_project`,
`editor_service.ensure_project_folder`, `editor_store.set_vod_upload`, `editor_service.vod_ready`,
`editor_service.free_space` and `gdrive.start_resumable_upload`; its project-creation route now
exists as `POST /api/uploader/projects` (secret-gated, in `OPEN_PATHS`; see "Gaming-PC uploader").

### Audit fixes (2026-10-30, `audit-fixes` branches)

A whole-codebase audit found and fixed, among others: the nightly backup now deletes `sessions` (and VACUUMs) in
its copy before upload, since a `drive.file` token can read every app file including backups;
`twitch_client` sends credentials as a form body and public routes never echo `str(e)` (a Twitch error used to
carry the client secret in its URL to homepage visitors); `_safe_next_path` refuses control characters
(`/%09/evil` became `//evil`); Cloudbot commands are built only for names that are one token of Unicode
letters/digits/marks plus `_ @ . -` (others are told points can't be used), and `cloudbot_reply_usernames`
(empty = any sender) limits whose replies count as Cloudbot's (YouTube display names are still forgeable);
the roulette agent allowlist entries are `platform:name` (a bare name is Twitch only); the Streamlabs tips
listener only starts when its switch is on; `widget_hub.broadcast` sends to a copy of the clients concurrently
with a 5 s timeout; the log is a `RotatingFileHandler` (5 MB x 5) and tests write to `DUALBLADEX_LOG_DIR`, never
the real `logs/backend.log`; greeter/go-live state goes through `config.atomic_write_text`.
`POST /api/editor/projects/{id}/download` records the click and returns `{"ok": true}` (no token).
Three commits that were pushed after their PR merged (net_time auto clock reset, past-due delete clamp,
keyless homepage videos) were re-applied: **always check a PR's head is the last commit before it is merged.**

### Editor-app Discord DMs: events -> notifier cursor -> outbox -> REST

No gateway, no library: `discord_dm.py` is two REST calls (create DM channel, cached in `meta` as
`discord_dm_channel_<id>`; post message, content truncated to 2000 with `…`). It never logs the
token, ids or content. A short 429 (`retry_after` <= 10 s) is waited out once; a stale cached channel
(404) is recreated once. **Nothing sends directly.** `notifier.run` (scheduler job) reads
`es.events_since(meta notify_cursor)` and, per event and inside one transaction with the cursor
advance, turns it into `notify_outbox.enqueue(role, text, dedupe_key)`; a failing event stops the
loop and retries next tick rather than skipping it. `dm_outbox` has a UNIQUE `dedupe_key`
(`ready:<max event id>`, `iteration_ready:<iid>`, `digest:<role>:<max event id>`, `reminder:<pid>:<hour>`,
`back_online:<prev last_seen>`), so replays are free. **While `discord_notifications_enabled` is
off, rows are created `dropped`** (key kept): switching it on later never replays old news. An empty
recipient id drops the row too (logged once at info). `deliver_due` claims a row `pending` ->
`sending` conditionally, sends, then `sent`; failure goes back to `pending` with
`scheduler.backoff_seconds(attempts)` (or Discord's `retry_after` if longer) and the error in `meta`
`dm_last_error`. **Accepted duplicates:** `reset_stuck_sending()` (startup) re-queues `sending`
rows, and a client timeout after Discord took the message retries - one DM may arrive twice; losing
one is judged worse. `status()` feeds the `discord` block of `GET /api/editor/storage`.
Switching notifications off also drops rows that were already `pending`. A new reminder supersedes a
project's still-pending ones (an outage never becomes a burst), and the notifier's first run baselines
its cursor at the newest event rather than DMing history. Events whose rows are gone are skipped with a
warning; a deterministic failure on an event stalls DMs (logged every tick) until fixed.

Rules: `vod_ready` -> editor, **grouped**: it waits in `digest_pending` under the pseudo-recipient
`ready` and flushes like a digest, so files that finish within `DIGEST_QUIET_SECONDS` of each other are
one DM ("8 new clips are ready: ...", each project once, deleted ones skipped); `iteration_ready` -> streamer, only when the actor is `system` or the
editor (so the streamer's own upload is not announced to himself). Comments, replies, resolves and
start-overs go to `digest_pending` for the *other* role (`_OTHER`; `system` actors notify nobody)
and flush as one DM per recipient once the newest of them is `DIGEST_QUIET_SECONDS` (600) old; a
digest names actor, tape, version and counts, **never comment text**. `reminders.run` DMs the editor
hourly from `REMINDER_DELAY_SECONDS` (3 h) after `download_clicked_at` while the VOD is `ready`, no
delete request is live, and `12 <= tm_hour < 22` (so the last of a day goes out before 22:00); its
next-due time is `meta reminder_next_<pid>`, pruned when the project stops qualifying.
`notifier.back_online_once` runs as a scheduler job (so only on sane ticks), once per process, using
the previous process's `clock_last_seen` that `main.py` read via `scheduler.last_seen()` *before*
`scheduler.start()` (the first tick overwrites it): a gap over `OUTAGE_SECONDS` (600) is one DM to
the editor, never a burst of catch-up messages.

**The bell** (`editor_bell.py`, routes `GET /api/editor/notifications?after=` and
`POST /api/editor/notifications/seen {up_to_id}`, both roles) is the same events log read directly:
only *other people's* events, never `system`, newest first, with an `unseen` count against
`meta bell_seen_<username>`. Comment/reply summaries are replaced by a kind-based label (no comment
text). `mark_seen` clamps to the newest event id, because a marker past it would hide every future
event. The bell works with Discord off.

**`db.ensure` now raises inside a transaction**, so any code that enqueues inside `db.transaction()`
must call `notify_outbox.ensure_schema()` (or the owning module's `_c()`) *first*; `notifier.run` and
`reminders.run` do this at the top. Plan 2 fixes carried here: `deletions` tells a clock jump from a
slow tick by comparing the wall-clock delta with the `time.monotonic()` delta, and
`complete_iteration_upload` refuses (`ensure_tape_not_deleting`, before and after its Drive await)
while a `tape_files` delete for that tape is live. Setup for the streamer: `DISCORD_BOT_SETUP.md`.

### Gaming-PC uploader: `/api/uploader`, secret-gated, idempotent by `job_id`

`uploader_api.py` has three routes, all behind `X-Agent-Secret` == `ocr_agent_secret` (`hmac.compare_digest`,
an empty secret refuses everything) and listed in `OPEN_PATHS`, since `/api/editor` needs a session.
`POST /api/uploader/projects` creates the project and a Drive resumable session (`appProperties
{"project_id": "<id>"}`) and answers `{project_id, kind, status, upload_url}`. **It is idempotent by the PC's
`job_id`** (`meta uploader_job_<job_id>` -> project id, written right after `create_project` with no await
between): a power cut before the PC saved the reply gets the same project back, never a second one. A session
is (re)made when the URL is empty or `new_session` is true (Google expires them after ~a week); `ready` answers
`upload_url: ""`, a deleting/deleted VOD answers 409 and the PC drops the job. `POST .../{id}/complete` is
`svc.vod_ready(id, file_id, "agent")` - actor `agent`, so the bell and digests show it to both users - and
repeating it is harmless. **VOD uploads are still not reconciled**: the PC retries `complete` until the
backend answers. `GET /api/uploader/live` asks Twitch itself (`twitch_client.stream_info`, cached `LIVE_CACHE_SECONDS`
60), falling back to `discord_live.live_state()` when Twitch fails or `twitch_channel` is empty; the PC
pauses on `true` and treats `null` as not live. The PC does not talk to OBS.

### Streamer.bot sends nothing until you subscribe

`streamerbot_client.py` sends a `Subscribe` request on **every** connect, not once at startup — a
subscription belongs to the socket, so a reconnect has to ask again. Without it the socket opens,
`is_connected` reports true, and zero events ever arrive; that silent failure is why
`is_subscribed` is tracked separately and surfaced on `/api/status`. The subscribed event set
defaults to `{"Twitch": ["ChatMessage"], "YouTube": ["Message"]}` (the two platforms spell their
chat event differently) and is overridable via `streamerbot_subscribe_events`.

Incoming payloads are **either** a request response (`status` + the `id` we sent) **or** an event
(an `event` object); responses are handled and logged, never fanned out to listeners. Chat
payloads go through `parse_chat_message()`, which accepts both the current shape (`data.user`
plus `data.text`) and the older one this backend was first written against
(`data.message.username` / `data.message.message`), because which arrives depends on the
Streamer.bot build on the gaming PC. Never read those fields directly in a feature module —
`forward_chat_to_widgets()` and `roulette.handle_chat_command()` share the parser precisely so
they cannot drift apart, and a mismatch here doesn't raise, it just silently stops every command.

`send_chat_message()` wraps the `SendMessage` request. Streamer.bot documents SendMessage as
requiring authentication on its WebSocket server, so it can legitimately come back non-ok;
`roulette_chat_replies_enabled` turns replies off wholesale rather than logging a rejection per
command.

### Widget fan-out is payload-agnostic

`widget_hub.py` owns the `/ws/widgets` connection set and exposes `broadcast()` / `send_to()`
with an optional `tag` for targeting. It decides nothing about message shape — feature modules
own their own payloads.

### The credit-OCR pipeline crosses both machines

Gaming PC (`agent.py`) → `POST /api/ocr/credit-report` with an `X-Agent-Secret` header →
`credit_ocr.py` runs Tesseract on the crop. Validation is four-layered: the expected label
text must be present, the value must be a multiple of 10 (Valorant economy values always are),
the value must not exceed `_MAX_CREDITS` (9000, Valorant's own cap — this catches the credit
glyph being OCR'd as a leading 1, turning 4200 into 14200; the glyph is also whitelisted in
`_TESSERACT_CONFIG` so Tesseract can emit it rather than being forced to guess a digit, which
is finding #1's comma bug repeating), and the consensus is **the newest reading in a 10-reading
rolling deque, unless it is an unconfirmed rise**.

That consensus rule has been wrong three times and the reasons are worth keeping. Majority vote
was first, and it reported the stale pre-spend value that several older readings agreed on
rather than the one genuinely final reading before the menu closed. The **minimum** replaced it
and was right for as long as one buy phase was one uninterrupted look, because "min next round"
only falls as you spend — but a refund raises it, and no minimum can follow a value upward, and
a single dropped digit (2400 read as 240) became a floor that held for the whole window. **The
first value seen twice, scanning back from the newest**, replaced that, and reported the
pre-purchase value on every fast buy: the reading that matters most is the last one before the
menu closes, and it is also the one with the fewest chances to be read twice — a real log
showed `6200` five times, `3300` once as the purchase landed, then nothing but blank frames,
and the reported consensus stayed on 6200.

The rule is now **asymmetric**, because the two directions are not the same risk. A reading
*below* the corroborated value is what spending looks like and is taken on a single sighting;
a reading *above* it is either a refund — which you keep shopping after, so the next frame
confirms it — or finding #4's inflating misread, and still waits for a second sighting. An
under-estimate shrinks the votable roster for one session; an over-estimate offers weapons
already paid for, which is the failure the feature exists to prevent, so **erring low is the
cheap side**. The known, deliberately unguarded hole: a dropped-digit misread arriving as the
very last valid frame is trusted — nothing in the data separates it from a real purchase, and
every guard that would catch it (exactly a tenth of the corroborated value, the corroborated
value with a digit deleted) rejects real buys too.

**Count over the scan so far, never over the whole window** — that is the part that looks like
an implementation detail and is not. Two OCR workers mean a reading can finish out of order and
land at the end of the window holding a pre-purchase value; the older readings that agree with
it sit *behind* it, so a whole-window count would let them vouch for a number the purchase had
already replaced. That straggler arrives *higher* than the value that replaced it, which is
exactly what the corroboration requirement still refuses. See findings #3, #7 and #8 in
`credit_ocr.py`.

**Every B press captures; Esc ends the burst; nothing else is guessed.** The buy menu is opened
with B, closed with Esc, and closed by the round starting when a purchase lands at the last
second. Two earlier rules tried to classify a B press as an open or a close and each broke
something. "Every press is a fresh start" fired on a B-toggle close and wiped the history that
phase had just produced. "B is a toggle, so a press during an active burst is a close" then
mistook a genuine re-open for a close whenever the previous close had been an Esc less than the
early-exit threshold ago, and swallowed the whole second look. `BurstTimer.trigger()` therefore
starts or restarts capturing unconditionally: a press that really was a close costs ~1.5s of
unreadable frames before the early exit notices, which is the cheap side of the trade.

**The early exit has two thresholds, and which one applies is decided by whether the burst has
read anything yet.** A run of 422s means "the menu has gone" only if the menu was ever there;
the same run at the *start* of a burst means the opposite — the menu's fade-in draws the panel
before the label is legible, and a B press that was really a close opens nothing at all. The old
single 1.5s threshold was slack chosen to outlast those; `ConsecutiveFailureTracker` now arms on
the first 200 and fires at `CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT` (10 → 0.5s) once armed,
falling back to `..._UNARMED` (30 → 1.5s, the old number, unchanged) when it never armed. Do not
delete the early exit on the grounds that Esc is always pressed — the round starting while the
menu is open is a close with no keystroke at all, and it is exactly the fast buy this pipeline
exists for. Without it those rounds capture until `burst_duration_seconds` (6) expires: ~120
unreadable frames, each a Tesseract run on a 2012 Mac Mini, while a `!roulette` may be reading.
`reset()` clears the armed flag as well as the count, so one look's reading cannot arm the next.

Esc *is* observable, and `agent.main()` hooks it: it calls **`BurstTimer.stop_capturing()`, not
`force_end()`**, and the difference is the whole reason the method exists. `stop_capturing()`
closes the capture window and leaves `_valid_until` open, so frames already sitting in the
thread pool's queue still get POSTed — they were all captured while the menu was open, and the
last of them typically holds the value the final purchase produced. `force_end()` closes both,
which is only correct for the early exit, where everything queued was captured after the menu
had already gone. The unreadable-frame run now covers only the two closes with no keystroke at
all. Only a genuine key **down-transition** may trigger either hook — `agent.main()` pairs
`on_press_key` with `on_release_key` for B and for Esc, since OS auto-repeat would otherwise
restart the burst dozens of times a second and discard each tick's captures moments after
taking them.

**Whether to reset the Mac Mini's history is decided by the clock, not the keystroke.**
`trigger()` returns True only when at least `burst_timer.NEW_ROUND_GAP_SECONDS` (20) has passed
since the previous press; that is what fires `/api/ocr/reset` and what bumps `buy_phase_id`. A
Valorant round cannot be won, ended and followed by a fresh buy phase inside twenty seconds,
while opening the menu twice within one buy phase easily happens — buy a rifle, close, re-open
to add armour — so a shorter gap means one buy phase and its earlier readings must be kept.

**A round ends when the next buy phase starts, not when a timer runs out.** Every capture and
every reset carries `X-Buy-Phase` (agent.py's fix #13), and `credit_ocr._begin_buy_phase()`
clears the window when that id changes — whichever of the two messages arrives first does the
clearing, and the other is recognised as already handled rather than wiping the readings the
new phase has just produced. That is what makes a dropped reset POST harmless.
`_READING_MAX_AGE_SECONDS` used to be pinned equal to `NEW_ROUND_GAP_SECONDS` (both 20) on the
reasoning that they were the same fact; **they were not**, and that mismatch is finding #9 in
`credit_ocr.py`. The gap is between two *presses*; the cutoff was the age of a *reading*. A buy
phase is read in about two seconds and the round after it runs well over a minute, so from
roughly twenty seconds after the menu closed until the next buy phase the dashboard said "No
reading yet" and the roulette opened its full roster, having read the budget perfectly well.
The cutoff is now a 300s backstop (`ocr_reading_max_age_seconds`) under two better signals, and
must never be tied back to the press gap. The other signal is `credit_ocr._agent_is_gone()`: an
agent that *has* been heard from and has since missed its heartbeat will never declare another
phase, so its window expires — but never having heard from one at all is deliberately not
"gone", since that is what an agent build older than the heartbeat route looks like.

Every capture is stamped with `BurstTimer.generation`, and the worker checks
`is_current()` before its network call: the thread pool's queue is unbounded, so without that
check a backlog of captures taken before the burst ended still gets POSTed afterwards (a real
log showed 9 such 422s after the early-exit message). **Every press bumps the generation**,
including one that continues the same buy phase — frames from before it belong to an earlier
look, and a consensus that prefers the newest reading must not receive them late.
`force_end()` (the early exit) takes no grace period: it only fires after a long run of frames
that read as nothing, so everything queued behind it was captured after the menu was gone.

**The capture rate is config-driven (`captures_per_second`, default 20) and everything derived
from it is expressed in SECONDS**, not in frame counts — `EARLY_EXIT_SECONDS` (0.5),
`EARLY_EXIT_UNARMED_SECONDS` (1.5) and `MAX_PENDING_CAPTURES` (1.2s of captures) are multiplied
by the rate at import. Writing them as counts was safe only while the rate was a constant:
turning it down would otherwise have turned 0.5s into 1s silently, which is exactly the mistake
the old comment warned about and could not prevent.

**`agent.grab_region()` captures only the calibrated box, never the whole screen.** It used to
be `ImageGrab.grab()` followed by a crop — a full 1080p grab to keep a 457×30 strip, twenty
times a second, on the machine also running the game. That is fix #15, and it is the answer to
"the OCR is costing me frames": the capture was, the OCR never was (Tesseract runs on the Mac
Mini).

**Three constants are one duration each, expressed in readings, and must be retuned
together**: `agent.CAPTURE_INTERVAL_WITHIN_BURST` (0.05s → 20 images/sec),
`credit_ocr._READING_HISTORY_SIZE` (20 → ~1s of consensus history) and
`agent.CONSECUTIVE_FAILURES_BEFORE_EARLY_EXIT` (10 → ~0.5s before assuming the menu closed —
the fallback for the two closes with no keystroke, a B-toggle close and the round starting,
now that Esc is hooked). They live in two repos; `test_agent.py` and
`test_credit_ocr.py` each pin the pairing from their own side. 422s are never appended to the
window, so it always holds the last 20 *valid* readings however many blank frames follow.
`agent.IDLE_CHECK_INTERVAL` (0.02s) is a fourth: it is dead time between the keypress and the
first screenshot, and at its old 0.5s a fast look could be mostly spent not looking.

**Raising the capture rate without capping the backlog makes things worse, not better.** The
send pool's queue is unbounded, so a rate the Mac Mini cannot retire does not raise the real
rate — it builds a backlog, and every frame in a backlog is delivered late, which is worse than
never taking it: the reading from the end of a burst is the one that decides the roster, and it
arrives after the roulette has already read the consensus. `agent.MAX_PENDING_CAPTURES` (24, ~1.2s
of captures) makes the loop *skip* a tick rather than queue it while that many sends are
outstanding, so over-asking degrades to the backend's real rate instead of to lag. The
matching lever on the other side is `credit_ocr._OCR_WORKERS`, now read from config
(`ocr_workers`, default 2) — raise it toward the core count if the agent starts printing
"Backend is behind", not past it.

Screen capture in `agent.py` is decoupled from the network round-trip via a bounded
`ThreadPoolExecutor` (`send_in_background`) — the screenshot itself is grabbed on the main
loop's precise tick, while the slow encode+POST+OCR round trip runs in the background, so
a slow Mac Mini response can't silently drag the real capture rate below the nominal one.
The Mac Mini end of that has the mirror-image rule: **`handle_credit_report` runs
`extract_credits` in `credit_ocr._ocr_executor`, never inline.** Tesseract is a blocking
subprocess call and this backend is one asyncio process, so calling it directly froze the
dashboard, the widget sockets and the chat handlers for the length of every capture — which is
also why raising the agent's capture rate used to change nothing. Two workers, not more: the
work is CPU-bound on a 2012 Mac Mini. Readings can therefore complete out of order, which a
minimum-based consensus does not care about.

`credit_ocr._last_reading` is a second, deliberately separate history that `handle_reset` does
**not** clear, surfaced as `credit_prediction.last_reading` and read by nothing that makes a
decision. The rolling window is empty between rounds by design, so on its own it cannot tell
"nothing yet this buy phase" apart from "this has never worked" — and the panel rendered both
as "No reading yet". A reading from a previous round is not a budget, so
`get_predicted_credits()` still returns `None` and the roulette still fails open.

Whether that agent is *running* is a separate question from whether captures are arriving, and
`ocr_agent.py` exists to keep them apart. Captures only travel during a burst - a couple of
seconds per round, nothing in between - so their absence is the normal mid-match state and can
never mean "the agent is down". `agent.heartbeat_loop()` therefore pings `/api/ocr/heartbeat`
every `HEARTBEAT_INTERVAL_SECONDS` (15) and the dashboard's OCR row reports *that*.
`ocr_agent.status()` still counts captures alongside it, because "alive but nothing readable"
and "not running" need different fixes and must not share one light; a recent capture also
counts as proof of life on its own, so an agent build predating the heartbeat is not reported
dead while it is visibly working. `credit_ocr.tesseract_available()` rides along on the same
row: with the binary missing every capture 503s, which from the gaming PC is indistinguishable
from a badly aimed region and from the dashboard would otherwise be invisible.

`affordable_weapons()` applies **two independent filters, and only one of them is about money**.
The **price** filter is built from `spendable_creds()` — the reading minus a shield and the
current agent's kit — and needs a credit reading, so it fails open on all three of its paths (no
prediction, the `roulette_affordability_filter_enabled` switch, a creds table that affords
nothing). The **sidearm** filter drops `PISTOL_WEAPONS` minus `ALWAYS_VOTABLE_PISTOLS` (i.e.
everything but the Sheriff) off a non-pistol round, because eighteen options is more than a chat
can read off an overlay in eighteen seconds and four of them are guns nobody wants to be forced
into with 4000 creds in the bank.

**The sidearm trim does not depend on the credit reading**, and used to only by accident of
sitting inside the priced branch — so with OCR off or down, every pistol came back onto the
wheel, which is the opposite of what the setting says. It now runs on every path.
`is_pistol_round(None)` is `False` by design: the *reading* is what is missing, not the round,
and a wheel full of Classics is worse than one built on a guess about which round it is.

**Order still matters and the empty check is still load-bearing**: the trim runs last and is
discarded if it would empty the roster, because on a save round the only affordable weapons
*are* pistols. `is_pistol_round()` reads the same `roulette_pistol_round_max_creds` the reserve
does, so the two can never disagree about which kind of round it is.

`roulette.status()` is what the admin dashboard reads, and it keeps three things apart on
purpose: the live session, `last_result` (which outlives both the session and the badge —
"which gun am I being forced into" gets asked during the buy phase, after the overlay has spun
and gone), and the forced-buy badge's own state machine.

`credit_ocr.get_predicted_credits()` is the consumption point for the prediction, and
`roulette.trigger_roulette()` is its only caller. It reads the prediction **once per session**
and snapshots the votable weapons into `_state.votable_weapons`; `vote()` checks that snapshot,
never a fresh prediction, so a consensus that keeps dropping mid-window can't retroactively
invalidate a vote someone already paid points for. The whole filter **fails open** — a `None`
prediction (OCR down, history just reset, no buy phase seen yet), the
`roulette_affordability_filter_enabled` config flag, and a creds table that would leave nothing
votable all fall back to the full roster. `WEAPON_CREDS_COSTS` holds Valorant's own in-game
prices, which is a different thing from `roulette_weapon_base_costs` (what a *vote* costs a
viewer in channel points); Riot retunes gun prices between patches, so every entry is
overridable via `roulette_weapon_creds_costs` in `config.json`.

### Overwolf was removed (2026-10-28)

`game_events.py`, `/api/game/state`, the Status panel's "Live game" tile and `gaming pc/overwolf-app/`
are gone (stream-backend#8, stream-site#3, pc-ocr#2). It was never run: Overwolf's game hook costs
frames on this PC. The B-press OCR signal is the only buy-phase source; recoverable from git if the PC
is ever replaced.

### The points wallet is Cloudbot's, and the REST API is a different one

`points.py` is now a thin seam in front of one implementation, `points_cloudbot`. Cloudbot owns
the balance viewers actually have — the one `!points` reports and watch time accrues into — and
it has no API, so `points_cloudbot.py` gets at it by posting `!addpoints`/`!removepoints` through
Streamer.bot and reading Cloudbot's replies back out of the chat stream. What `points.py` still
contributes is `UnknownUser` and the two locks, and the spend lock is load-bearing: Cloudbot
replies are matched to commands by `(platform, username)`, so two overlapping `!removepoints`
for one viewer could be answered in either order with no way to tell which is which.

Two other backends used to live here and **both are deleted** (recoverable from git): `api`,
Streamlabs' Loyalty Points REST API, and `local`, a flat JSON file for offline testing. The
`points_backend` config key, the dashboard's ledger switcher (`usePointsBackend.ts`) and the
Status panel's ledger row went with them.

**`api` was not that wallet, and believing it was cost a day.** Streamlabs' Loyalty Points REST
API sat behind a manual approval for months; when it was granted, a REST read of the whole
channel's loyalty list returned `total: 0` while the Cloudbot dashboard showed every real viewer,
and a `user_point_edit` write for a username that existed nowhere came back with
`"platform": "points"` — not `twitch`, not `youtube`. That row is visible over REST and absent
from Cloudbot's list. **Two stores.** Passing `platform` explicitly is the obvious next idea and
does not work either — a write sent with `"platform": "twitch"` comes back `"platform": "points"`,
so the field is not an input and there is no way to address a Cloudbot row from these endpoints.
`api` is therefore `local` with a network hop: persistent, hosted, and holding nothing anyone
earned. Leave `points_backend` on `cloudbot`, and don't re-run this investigation.

The tips listener is off by default and must stay that way unless someone turns Cloudbot's own
donation payout off first: granting here means posting `!addpoints` into chat, and Cloudbot
already pays donors natively, so running both grants twice and puts a bot line in chat per tip.

### Spotify song requests need Premium, an active device, and nothing else

`!song <name or link>` searches, or reads a pasted Spotify link, and queues onto the streamer's
own Spotify (`!sr`, `!songrequest`, `!request` are aliases); `!songqueue` shows what is coming,
free. There is deliberately **no chat command for "what is playing"** — that has its own overlay,
and a command answering a question already on screen is a command spent on nothing. The queue is
the exception: what is *coming* is not on the overlay, and it is what someone asks right after
paying. Its entries are trimmed to `_QUEUE_ENTRY_MAX_CHARS` so three of them fit inside
YouTube's 200-character limit — real titles are long enough that untrimmed entries overflow, and
`test_spotify.py` pins the whole line against that limit. `spotify.py` holds the API client, the
viewer-facing flow and the overlay feed; `spotify_oauth.py` the one-time connect.

Three of Spotify's constraints are non-negotiable and two of them are the failures you will
actually see: **Premium** (the queue endpoint 403s without it) and an **active device** — the
queue targets whatever the account is currently playing on, so Spotify has to be *playing*, not
merely open. That returns a 404 carrying `NO_ACTIVE_DEVICE`, which gets its own chat message
because "start playing something first" is actionable and "request failed" is not.

**Development Mode is sufficient and Extended Access is not needed**, which is the opposite of
the Streamlabs Loyalty situation. From 9 March 2026 a dev-mode app is capped at five users and
needs the owner to hold Premium; this app controls exactly one account, so the cap is irrelevant,
and Extended Access is now reserved for "established, scalable" products and would be refused.
The endpoints withdrawn from dev mode are the batch and browse ones — the Player API is not
among them.

The refresh token is the durable credential (access tokens last an hour and are memory-only,
refreshed on demand under a lock so a burst on a cold token cannot start several refreshes at
once). **Spotify may issue a new refresh token on refresh, and the old one then stops working** —
`access_token()` saves it, and without that song requests would work until the next restart and
never again.

**Points are taken after the track resolves and refunded if the queue call fails.** Resolving
first matters: charging and refunding for a typo puts two entries in a viewer's history for
nothing and makes their mistake look like a fault. `spotify_oauth` sends `show_dialog=true`,
without which re-running the flow to add a scope silently returns a token with the OLD scopes
and fails later at the queue call with a 403 that looks like a Premium problem.

One collision worth knowing: `roulette.handle_chat_command`'s catch-all used to treat any `!word`
during a live session as a mistaken vote. `!song blinding lights` would have been answered "that
isn't a recognized weapon", so the catch-all now fires **only when the whole message is one
word** — a vote is always bare.

`widgets/spotify-now-playing.*` is the third backend-fed overlay, on tag `spotify`. The backend
polls Spotify and broadcasts; the widget never holds a credential. Two things follow from
owning both ends: the overlay can name **who requested** the current track (`_recent_requests`,
keyed by URI — nothing off the shelf knows a request system exists), and the **whole payload is
re-sent every tick** rather than only on change, so a Browser Source reload recovers within one
poll instead of sitting blank until the next song — the limitation `forced-buy-badge.js` still
documents. The poll is skipped entirely while `widget_hub.connected_count("spotify")` is zero,
so a backend left running overnight does not spend it asking Spotify what is playing. The
progress bar is interpolated client-side from the last reading and clamped to the duration; a
paused track does not advance.

That overlay is the one place the fixed palette is deliberately abandoned: `--art-color` is read
off the album cover at runtime (a 12px canvas, letting the browser do the averaging, with
near-black and near-white pixels skipped so every sleeve doesn't average to the same grey) and
`liftForContrast` saturates it, because a raw average is always duller than the cover looks and
a dull colour on a dark card is invisible. **Reading those pixels can fail and failing is
normal** — a cross-origin cover taints the canvas, and OBS loads these overlays as local files —
so it is wrapped and falls back to the teal rather than leaving the card colourless.

### `!rank` and the greeter are the two listeners that are not roulette

`valorant_rank.py` answers `!rank` / `!rr` / `!elo` from the same unofficial HenrikDev API the
rank overlay uses — for anyone watching on a phone or with the overlay cropped out. **The
credentials live in `config.json` (`henrik_api_key`, `riot_name`, `riot_tag`), not in the
module**, unlike `rank_session_tracker.html`, which still carries its key in plaintext in a file
copied to the OBS machine by hand. Cached for 60s because one person asking prompts three more
and HenrikDev is a free community API this project has no claim on; a rank cannot change in
under a match, so nothing is lost. A **failed** fetch is deliberately not cached — one blip
would otherwise silence the command for the whole window — and every failure answers in chat,
because a command that does nothing is indistinguishable from a bot that is down.

`greeter.py` posts the Discord invite to a **first-time chatter, once**. The pattern it replaces
is Nightbot's timer, which drops the link every N minutes and so shows it to the regulars —
the people who already joined — forty times a stream.

Three decisions worth keeping. **"First" means first ever**, so the seen list is persisted
beside `config.json`: a restart mid-stream must not re-welcome the whole chat, which is exactly
when it would look broken. **Streamer.bot's `first-msg` flag is not the mechanism** — Twitch
sends it and YouTube sends nothing of the kind, so building on it would mean the feature
silently working on half the audience; it is read when present but can only ever *add* a
greeting, never suppress one. And the cooldown is **global, not per-user**: per-user is already
covered by only ever greeting someone once, so what it actually guards is the raid, where forty
newcomers in ten seconds would mean forty invite links. Everyone past the first in that window
is **recorded as seen and not greeted** — greeting them an hour later, when they are no longer
new, would be stranger than not greeting them.

Off by default, and `is_enabled()` requires `discord_invite_url` as well as the flag: enabled
with nothing to link to would post "welcome in! Come hang out in the Discord: ".

### `health_checks.py` is the one status check that leaves the machine

Every other field on `/api/status` is introspection - a variable read out of this process. Whether
the public hostname still reaches this process is not: that request goes out to Cloudflare, back
down the tunnel and in through `/health`, and every hop can be broken while the backend is fine.
`health_checks.probe_once()` does exactly that round trip against `config.json`'s `public_base_url`,
on its own 30-second timer, so opening the dashboard never waits on the network and hammering
Refresh cannot become a burst of outbound requests.

`/health` answers with an `instance` token - `secrets.token_hex(8)`, regenerated every start, not a
secret and not used for auth. It is the whole point of the check: a `cloudflared` left running by a
previous backend, or a DNS record still aimed at another machine, answers `{"status": "ok"}` just as
cheerfully as the real thing, and only the token separates "the public URL reaches me" from "the
public URL reaches something". That distinction matters because the OBS widgets connect over the
same public hostname, so a misdirected tunnel surfaces as zero widget connections and nothing else.
`probe_once` takes an injected session factory for the same reason the timing code does - every
branch (timeout, refusal, wrong status, wrong instance) is reachable in a test with no network.

### `widgets/` splits into backend-fed and self-contained

`roulette-overlay` and `forced-buy-badge` connect to the Mac Mini's `/ws/widgets?widget=roulette`
and render backend-broadcast events verbatim (`roulette_started`, `weight_updated`,
`invalid_vote`, `unaffordable_vote`, `roulette_ended`, `forced_buy_queued/active/cleared` —
matches `roulette.py`'s broadcast call sites exactly). The **pie is the wheel, not the vote
tally**: `wheelShares(weights, baseShare)` gives every weapon on the roster `base` (1) plus its
votes and `computeSlices()` draws that, so one vote for the vandal doubles its slice and shrinks
the rest rather than eliminating them — which is exactly what `roulette.wheel_shares()` /
`draw_winner()` do on the backend, and the two must stay the same arithmetic. An unvoted session
is therefore a genuinely uniform wheel, because that is what it is. The **legend** is the same
roster, built by `legendRows()` from `roulette_started`'s `weapons` list, so a pistol round
visibly offers five options where a full buy offers thirteen. `legendRows(null, ...)` falls back
to voted-weapons-only so an older backend that sends no `weapons` key still renders.
`rank_session_tracker.html` is the other kind: it talks directly to `valorant-api.com` and
`api.henrikdev.xyz`, with no Mac Mini involvement at all — the same split `widget_hub.py`'s
module docstring describes. **It no longer connects to OBS at all.** It used to ask
obs-websocket when the stream went live, with a YouTube Data API lookup behind it as a
fallback; both are gone, along with the OBS password and the YouTube key that lived in its
source. The Valorant scene is not open while another game is being played, so the scene loading
the page *is* the start of a session — with a `SESSION_RESUME_WINDOW_MS` (30 min) grace period,
because a Browser Source set to refresh on scene activation reloads on every scene switch and a
switch mid-stream must not wipe the session. The session RR swing is computed from
`absoluteRR = tierId * 100 + rr`, so a promotion reads as `+14` rather than `-86`.
Both backend-fed widgets share a fixed-reference-size-plus-CSS-transform-scale pattern
(`REFERENCE_WIDTH`/`REFERENCE_HEIGHT` in the `.js`, matched by `#scale-stage`'s fixed size in
the `.css`) so OBS's native resize handles work without pixel-exact Browser Source dimensions.
**Those two must be changed together** — the roulette overlay is 580×360 in both. `widgets/` is
the private repo `aryanverma7/stream-widgets` (since 2026-10-30): one folder per widget, the rank
tracker's HenrikDev key in git-ignored `rank-session-tracker/config.js`, `./run-tests.sh` runs every
widget test; deploy is `git pull` on the OBS machine plus a browser-source refresh. Themes live on
`theme/*` branches until the streamer picks one.

`widgets/horizontal-chat.*` is the fourth backend-fed overlay, on tag `chat`, fed by
`streamerbot_client.forward_chat_to_widgets` — which already merges Twitch and YouTube into one
stream, so each message carries an **inline-SVG platform icon** and a matching username colour,
with an unrecognised source rendering neutral rather than silently claiming to be Twitch.
Colour alone was tried first and is not enough: two brand colours at 14px going past at speed
are indistinguishable, so the icon carries it and the colour reinforces it. The glyphs are
inline rather than fetched, because an overlay that needs the network to say which chat someone
is in says nothing when the network blips.

**Emotes are forwarded, not assumed.** `_parse_emotes` accepts several payload shapes for the
same reason `parse_chat_message` does — the emote array is the least stable part of the payload
across Streamer.bot builds and across the two platforms — and an entry with no usable image URL
is **dropped rather than guessed at**: a Twitch id maps to a CDN path by documented formula, a
YouTube one does not, and a broken image in an overlay is worse than a word. The widget splits
on start/end indices when they are present and on whole words when they are not, since builds
report one or the other. `streamerbot_log_raw_chat` dumps a whole chat event to the log — off
by default, and the only reliable way to learn what a given build actually sends, which is how
each of the accepted shapes was discovered. Three rules it does not share with the others: it
scales by **height only, clamped to 3x** (a fixed-box `min(w/refW, h/refH)` would letterbox a
full-width ticker and leave most of the canvas empty — but height-driven scaling with no ceiling
is a trap, because OBS creates a Browser Source at **800×600** by default, which is 9.4× here,
at which the rail is 85 unscaled pixels and holds less than one message: one giant pill in the
corner of an empty box, which reads as a broken widget rather than a mis-sized one. The row is
also centred vertically rather than pinned to the top, since a clamped scale no longer fills a
tall source); **nothing is ever set with innerHTML** — chat is
attacker-controlled text arriving over a socket into a Browser Source, so every value goes in
through `textContent`, which `horizontal-chat.test.mjs` pins directly; and **messages are never
removed on a timer**. They hold position until new ones push them off the left edge, so a quiet
chat stays put and a busy one scrolls. An earlier version expired them after 45s, which emptied
the row during every quiet stretch and read as "chat is dead". `#rail` is `width:max-content`
inside a clipping `#viewport` and is translated; `prune()` drops only what is fully out of sight
and **re-anchors the offset in the same frame with the transition off**, or removing something
invisible would yank everything visible sideways by its width.

`widgets/break-screen.html` is the fifth, on tag `break`, driven by `break_timer.py`. It is a
**single file** like `starting-soon.html` and unlike the small overlays — a full-canvas scene is
a scene, not a component, and one file is one thing to hand-copy. It is also deliberately *not*
the starting-soon HUD with a new heading: that screen is tactical (hard grid, corner brackets,
scanlines, glitch, scrolling ticker) because it counts down to something starting, and a break
is the opposite mood, so this one is ambient — drifting aurora, no grid, no brackets, no ticker,
no glitch. **The tide is the progress bar**: two sine waves whose surface sits at the fraction
of the break remaining, eased toward the target rather than set, so a broadcast correcting the
time by a second slides instead of stepping. There is no second progress indicator. The duration
is set from the admin dashboard's Break tab **before** switching to that scene, because once it
is live it is the only thing on stream. State is in memory, not config: a break is something
happening now, must not survive a restart as though still running, and `started_at` +
`duration_seconds` is all of it — remaining time is always derived, never stored, so it cannot
drift. Starting again *is* "add five minutes"; there is no separate verb. The screen **never
counts past zero** — it switches to "any moment now" and stays up, because hiding it would put
the streamer on camera before they are there and counting up would be a stopwatch of how late
they are. Like the Spotify overlay it is re-broadcast every few seconds rather than only on
change, so a Browser Source reloaded mid-break resyncs instead of sitting blank until somebody
touches the dashboard — which, during a break, is nobody.

`roulette-overlay.test.js` and `rank-session-tracker.test.mjs` (`node rank-session-tracker.test.mjs`,
which loads the page's script into a `vm` with a stubbed DOM, clock and fetch) are the only tests
here; `forced-buy-badge.js` has none. The roulette
overlay's own logic is deliberately kept in pure functions (`computeSlices`, `computeScale`,
`legendRows`) so it can be exercised by loading the file into a Node `vm` context with a stubbed
`document`/`WebSocket` — that is how the roster work was verified, but the harness was throwaway
and is not in the tree either.

### Frontend talks to the backend, never to itself

`output: "export"` in `next.config.ts` — there is no Next.js server in production. Every
dynamic behavior is a client-side `fetch()` or WebSocket call to the Python backend, which
serves the built `out/` directory directly from the path in `config.json`'s `site_out_dir`
(an absolute macOS path). Data fetching is consistently factored into `lib/use*.ts` hooks,
one per concern, each with its own test; components stay presentational.

Navigation to `/admin` is a deliberate exception to client-side routing: `Dragon.tsx` calls
`window.location.assign("/admin")`, not `router.push`. `/admin` and everything under it is gated
by `auth.py`, which answers a session-less visitor with a `302` to `/auth/login` for every
`/admin/*` and `/editor/*` path — the document and the router's nested route-data fetches alike
(`path_matches` is whole-segment). Only `/api/*` gets a JSON `401`. The router cannot follow that
login redirect on a data fetch, so the gesture does a real document navigation, which can; the two
pages share no state, so it costs nothing.

That redirect has to carry its destination through GitHub and back. `handle_login` puts the
requested path in the OAuth `state` parameter (`_pending_logins` maps the random state value
to the path) and `handle_callback` redirects there instead of to `/`. Without it the callback
always landed on the homepage, so the hidden dragon gesture opened the login flow and then
dropped the person back where they started with a valid session but no dashboard — the
gesture had to be performed a second time to get anywhere. `state` is doing double duty: it
is also the OAuth CSRF check, and a callback whose state the backend did not issue is
rejected outright. `_safe_next_path()` refuses anything that isn't a local path, since
`//evil.example` is a perfectly valid `Location` header meaning `https://evil.example`.

## Non-obvious constraints

These were each established the hard way; changing them will re-break something.

- **`streamerbot_ws_url` must be the gaming PC's LAN address, not `localhost`.** The Mac Mini
  dials *out* to Streamer.bot, which runs on the other machine; `config.example.json` ships
  `ws://localhost:8080/`, which is only correct if both were the same host. Streamer.bot's
  WebSocket server also binds to loopback by default and needs `0.0.0.0` plus a firewall rule
  before the connection can be made at all. Full walkthrough in `mac mini/Stream/STREAMERBOT_SETUP.md`.
- **Streamer.bot's WebSocket authentication is on, with Enforce on, and the handshake order is
  load-bearing.** `SendMessage` is the one request Streamer.bot marks "Authentication Required",
  so chat replies need it; Enforce extends the requirement to `Subscribe`, which is what stops
  anything else on the network reading chat through that port. Consequently
  `_connect_and_listen` must **not** subscribe on connect — it waits for the server's `Hello`,
  answers the challenge, and subscribes only from the `Authenticate` response. Reordering those
  back gets `Subscribe` rejected outright on an enforcing server. The hash is the obs-websocket
  scheme: `base64(sha256(base64(sha256(password + salt)) + challenge))`, keyed on
  `streamerbot_ws_password`.
- **`is_authenticated` is tri-state, and `None` is not a failure.** `None` means Streamer.bot
  never issued a challenge (its Authentication toggle is off); only `False` means one was
  answered and refused. `/api/status` and `StatusPanel` both branch on `=== false`, never on
  falsiness — the same optional-field rule the rest of `AdminStatus` follows.
- **`python-socketio` is pinned to 4.6.1, not 5.x.** Streamlabs' server speaks Engine.IO
  protocol v3. Version 5.x targets v4 and will not connect.
- **`Pillow` is pinned to 13.0.0 on the Mac Mini** (earlier releases predate Python 3.14 and have
  no wheel); the gaming PC stays at `Pillow>=11.0.0`. `aiohttp` is 3.14.5. `conftest.py` shims
  `aioresponses` (0.7.9) for aiohttp 3.14 - test-only; remove it when aioresponses ships a fix.
- **Tesseract lives at `/opt/local/bin`** (MacPorts, not Homebrew — Homebrew's formula hits a
  python@3.14 bootstrapping bug on this unsupported-Catalina config). The path is hardcoded
  because launchd runs with a minimal PATH that never sources `.zshrc`.
- **Google Fonts load via a plain `<link>` in `app/layout.tsx`, never `next/font/google`**,
  which fetches and self-hosts at build time and needs build-time network access.
- **Timing-sensitive code takes injected clock/scheduler functions.** Global fake-timer
  mocking of `requestAnimationFrame` was found unreliable for driving recursive tick loops.
- **`vitest.config.ts` needs its own `@` path alias.** Vitest resolves modules through Vite,
  not through `tsconfig.json`.
- **The agent's heartbeat interval and the backend's staleness cutoff are one setting living in
  two files.** `agent.HEARTBEAT_INTERVAL_SECONDS` (15, gaming PC) and
  `ocr_agent.HEARTBEAT_TIMEOUT_SECONDS` (45, Mac Mini) are chosen so three pings fit inside the
  cutoff - two may be dropped before the dashboard calls the agent dead. Change one without the
  other and the panel either flaps on a single lost packet or takes minutes to notice a real
  outage. `test_agent.py` asserts the ratio.
- **`public_url.reachable` is tri-state like `streamerbot_authenticated`, and `null` is not a
  failure.** `null` means the check has no address configured or has not run yet; the panel reads
  "Not checked", never "Unreachable". Same `=== false` rule as everywhere else here.
- **New `/api/status` fields must be optional on the frontend's `AdminStatus` type.** The Mac
  Mini routinely runs a backend older than the built site, so `StatusPanel` degrades to a plain
  "not reporting" line rather than rendering `undefined`s — the same rule the roulette overlay's
  missing-`weapons` fallback follows.
- **`useAdminStatus` polls, and `loading` is only for fetches a person is waiting on.** The
  panel re-reads `/api/status` every `STATUS_POLL_INTERVAL_MS` (5000) — it is open on a second
  monitor while things are restarted on two other machines, and every change used to have to be
  discovered by clicking. A background poll must not set `loading` (the Refresh button would
  read "Refreshing..." forever, which looks like a fault) and must not blank `status` on
  failure (a backend restart would erase the last known answers, which are the most useful
  thing on screen). Polling is skipped while `document.visibilityState === "hidden"` and fires
  immediately on `visibilitychange` back to visible — the page sits behind OBS for most of a
  stream. `/api/status` answers `Cache-Control: no-store` for the same reason: nothing on that
  response carries a validator, and a heuristically cached status panel is not a stale page, it
  is a wrong answer about whether the stream is ready.
- **The dashboard must never re-derive the affordable roster.** `dashboard_api._credit_prediction()`
  calls `roulette.affordable_weapons()` so the panel cannot disagree with what the roulette will
  actually do, including all three fail-open paths.
- **The test suite must never touch the real `config.json`.** `conftest.py` writes a dummy config to a
  temp folder and sets `DUALBLADEX_CONFIG` (read by `config.py`) *before* anything imports `config`.
  Tests monkeypatch `config._data` and many code paths call `config.save()`, so before this (fixed
  2026-10-28, stream-backend#6) a `pytest` run on the Mac Mini overwrote the live config with test values
  and the next restart lost every setting and secret. `tests/test_config_isolation.py` pins it.
- **OBS widgets are freely resizable by design**, using a fixed reference size plus a JS
  transform-scale, so they don't need pixel-exact Browser Source dimensions.
- **The hero needs runtime internet.** `Dragon.tsx` pulls Three.js r70, the original CodePen's
  `OrbitControls.js`, and TweenMax 1.16.1 from `cdnjs`/`s3.cdpn.io` as `next/script` tags, in
  that dependency order, gated on React state rather than tag order. The `three`, `@types/three`,
  and `gsap` entries in `package.json` are **not imported anywhere** — nothing in the tree does
  `from "three"` or `from "gsap"`.

## Environment note

**`mac mini/Stream` and `mac mini/Site` are real clones** of `aryanverma7/stream-backend` and
`aryanverma7/stream-site` (their `.git` was dropped into the existing folders on 2026-10-25, so
the working tree is what was already here). Commit and push from inside each folder. Their
`.gitignore`s do **not** cover `*:Zone.Identifier`, so never `git add` a directory wholesale —
stage files by name, or the Windows metadata files go in too. `gaming pc/` and `widgets/` are
still plain copies (no `.git`): ship those by cloning into a scratch directory, copying, and
pushing. `gh` on this machine is authenticated as `aryanverma7` with `repo` scope. Editing a
file only reaches the machines after a push and a pull there — never hand out `git pull`
instructions without having pushed first.

This tree is a WSL/Linux copy of code that runs on macOS and Windows. `mac mini/Stream/venv`
holds macOS-native binaries, and there is no working `pip`/`venv`/`ensurepip` on this machine
and no passwordless sudo to install them (`python3 -m pip` → "No module named pip"; `python3 -m venv`
→ "apt install python3.12-venv"). The backend suite does run here, through
uv (the command is under "Backend" above); 12 tests in `test_credit_ocr.py` fail because there is
no `tesseract` binary on this machine. `gaming pc/`'s suite (`pytest`/`Pillow`) has not been tried
under uv. `mac mini/Site/node_modules` was macOS-native too, but a plain
`npm install` in WSL replaces it with Linux binaries and both `npx vitest run` and
`npx next build` then work normally — do this if the frontend suite reports a native-binding
error. A stale `.next/` build cache carried over from macOS can also fail Turbopack outright
(`Failed to open database` / `Loading persistence directory failed`); `rm -rf .next out` fixes it.

## Known gaps (as of last audit)

- `mac mini/Stream/README.md` (titled "Task #3 Skeleton", documents only the skeleton)
  describes an earlier, now-inaccurate state — see "Non-obvious constraints" above for what actually holds.
- The repo's `com.dualbladex.backend.plist` still has literal `/path/to/mac_mini_backend`
  placeholders and `/usr/bin/python3`, but the **installed** copy on the Mac Mini
  (`~/Library/LaunchAgents/`, checked 2026-10-26) is correct: `venv/bin/python3` +
  `main.py` with `WorkingDirectory` `~/Documents/Stream`. Fix the repo copy to match if it is
  ever reinstalled from the repo.
- The editor-app routes (`/api/editor/*`) have no frontend yet (Plan 5); only the Drive connect and the Settings
  keys are reachable from the site today.
- `widgets/` has no git remote, so nothing there is deployed by a pull — it is a manual copy to
  the OBS machine plus a browser-source refresh. `forced-buy-badge.js` has no test and is
  untouched by the roulette-overlay work.
- `points.py`, `site_server.py`, `widget_hub.py`, `server.py`,
  `logger.py`, and `main.py` have no test coverage (`dashboard_api.py`'s config routes are covered by `tests/test_dashboard_config.py`). `ocr_agent.py` and `health_checks.py` have
  `tests/test_ocr_agent.py` (10) and `tests/test_health_checks.py` (15). `auth.py` has
  `tests/test_auth.py` (34 tests) covering the `OPEN_PATHS` allowlist, roles, sessions, the `state` round trip, and the open-redirect
  refusal — that allowlist is the one thing the state doc says broke in production twice. On the frontend the
  untested files are `lib/parseChatMessage.ts`, `components/Dragon.tsx`, `components/Hero.tsx`,
  and `components/TwitchEmbed.tsx`.
- `mac mini/Stream/config.json`, `gaming pc/agent_config.json`, and
  `widgets/rank_session_tracker.html` all hold live plaintext secrets (OAuth secrets, API keys)
  — `config.json` is git-ignored in the Stream clone; the other two sit in folders with no
  `.git`/`.gitignore` to keep them out of a future commit. The OBS
  WebSocket password and the YouTube API key are gone from the rank widget along with the
  connections that used them; its `henrikApiKey` is still there in plaintext.
- Foundation.md's Tasks 3–7 (hold-gesture, raw WebGL shader, GLSL source, `ShaderBackground`,
  `HiddenGateway`) were never built; `Hero.tsx` wraps a Three.js/GSAP-based `Dragon` component
  instead, which contradicts the doc's own "no Three.js" constraint. Foundation.md's checkboxes
  are still all unchecked and should not be treated as a live task list without reconciling this.
  Concretely absent: `lib/holdGesture.ts`, `lib/webgl.ts`, `shaders/vertex.ts`,
  `shaders/fragment.ts`, `components/ShaderBackground.tsx`, `components/HiddenGateway.tsx`, and
  all their tests. The gateway *behavior* does exist, via a different mechanism: charging the
  dragon to a max sneeze fires `window.__onDragonMaxSneeze`, which flashes and
  `router.push("/admin")`s — not the spec's 2.5s invisible-hotspot hold with a progress ring.
- ~1,700 `*:Zone.Identifier` files (Windows mark-of-the-web metadata) are scattered through the
  tree. They're inert, but they will pollute globs and a first `git add`.
