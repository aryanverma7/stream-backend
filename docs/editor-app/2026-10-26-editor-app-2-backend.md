# Editor App — Plan 2 of 5: Editor backend (data, Google Drive, deletes, power-cut recovery)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the editor app stores and every Drive operation it performs, exposed as `/api/editor/*`: projects, tapes, iterations, comments with threads and carry-over, per-user to-dos, read state and playback position, the Drive connection, iteration uploads straight to Drive, two-step deletes with a one-hour undo, and recovery from power cuts.

**Architecture:** New backend modules in `mac mini/Stream` on top of Plan 1's `db.py` (one shared SQLite connection, on the event loop only) and `auth.py` (admin/editor roles). Large files never pass through the Mac Mini. The backend creates Drive resumable-upload sessions and hands the browser only the session URL. Every timed thing is an absolute time in SQLite, driven by one scheduler loop behind a clock-sanity gate. No frontend in this plan (Plan 5). No Discord (Plan 3). No uploader or agent route (Plan 4).

**Tech Stack:** Python 3.14, aiohttp 3.14.5 (also the HTTP client for Google), stdlib sqlite3, pytest + pytest-asyncio + aioresponses.

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md` ("Data model", "Flows" 2/3/6, "Deletion rule", "Power cuts", "Decisions made after the builder check"). **Roadmap:** `docs/superpowers/plans/2026-10-25-editor-app-roadmap.md`, Plan 2 rows plus "What each plan hands on → Plan 1".

## Global Constraints

- Repo: `mac mini/Stream` is a real clone of `aryanverma7/stream-backend`. Work on branch `editor-app-plan-2` (already created from `main` at 7656ae8). **Stage files by name**, never a directory (`*:Zone.Identifier` files are not git-ignored). Never push; the controller pushes.
- Tests, from `mac mini/Stream` (referred to as **`PYTEST`**): `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings`. Baseline: 595 passed, 12 failed (all in `tests/test_credit_ocr.py`, which needs tesseract). "Passes" means no failures outside that file.
- Async tests carry `@pytest.mark.asyncio`. Google HTTP is mocked with `aioresponses`, as in `tests/test_streamlabs_oauth.py`.
- No new Python dependency. All Google calls go through `aiohttp`.
- DB rules from Plan 1: use `db.get()` only on the event loop; **never hold a transaction open across an `await`**; tables are created with `db.ensure(ddl)` called lazily (not at import); tests call `db.use_path_for_tests(tmp_path / "x.db")` in an autouse fixture.
- Timing code takes `now: float | None = None` (default `time.time()`), and tests pass it explicitly.
- Every new config key gets a `config_schema.FIELDS` entry (section `"Editor app"`) and a line in `config.example.json`. Secrets have type `"secret"`. `test_every_example_key_has_metadata` enforces this.
- Routes: every new route is under `/api/editor/` (reachable by both roles per `auth.EDITOR_PATHS`) except the Drive connect routes `/auth/gdrive/login` and `/auth/gdrive/callback`. Those are admin-only by default, because they are not in `EDITOR_PATHS`, and must **not** be in `OPEN_PATHS`. Admin-only actions under `/api/editor/` check the role inside the handler and answer `403 {"error": "Only the streamer can do this."}`.
- Google constants (exact):
  - `SCOPE = "https://www.googleapis.com/auth/drive.file"`
  - `AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"` with `access_type=offline` and `prompt=consent`
  - `TOKEN_URL = "https://oauth2.googleapis.com/token"`
  - `API = "https://www.googleapis.com/drive/v3"`
  - `UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"`
- Deletes are **permanent**: `DELETE {API}/files/{id}`, never a trash update. A `404` from that call counts as success.
- Values (exact):
  - undo grace `DELETE_GRACE_SECONDS = 3600`
  - scheduler tick `TICK_SECONDS = 30`
  - retry backoff `min(30 * 2**attempts, 3600)` seconds
  - an iteration upload still `uploading` after `ABANDON_AFTER_SECONDS = 8 * 86400` becomes `abandoned`
  - backup daily at `BACKUP_HOUR = 4` (local time) and keep the newest `BACKUP_KEEP = 7`
  - thumbnail ≤ `262144` bytes
  - checks JSON ≤ `16384` bytes
  - search returns at most `50` hits
- Drive folders created by the app: root `"DualBladeX Editor"` holding `"Projects"`, `"Brand kit"` and `"Backups"`. A project folder is named `"{stream_date} {name}"`, and each tape gets its own subfolder named after the tape. An iteration file is named `"v{version} - {filename}"` and carries `appProperties {"iteration_id": "<id>"}`.

## Review Focus

1. **Power cut between Drive deleting a file and the DB recording it.** On restart the file row is still `deleting`, the retry gets a `404`, and that must count as success. It must not fail forever. Pinned in Task 8 (`test_retry_after_crash_treats_404_as_done`).
2. **The Mac Mini boots with its clock in the past** (weak clock battery). No delete, backup or reconcile job may run until the clock passes the last time it wrote down. Pinned in Task 7 (`test_clock_behind_last_seen_blocks_jobs`).
3. **An editor session calling an admin-only action** (resolve a comment, mark Final, delete a tape or project, Storage, rename a project, undo a request he did not make) must get a 403, not do the action. Pinned in Task 11 (`test_editor_is_refused_admin_actions`).
4. **The Drive login gets revoked or expires** (the refresh fails). Every Drive call raises `DriveNotConnected`; pending deletes stay `pending` and are never marked done; `/api/editor/drive/status` reports `connected: false`. Pinned in Tasks 3 and 8.
5. **An upload finishes while the Mac Mini is down, or `complete` is called twice.** Reconcile registers it from the tape folder, and completing twice is harmless: one carry-over, one event. Pinned in Tasks 9 and 10.

---

### Task 1: DB path fallback and a key/value `meta` table

**Files:**
- Modify: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.get()` falls back to `DEFAULT_PATH` when the configured `app_db_path` cannot be opened. That covers a missing parent directory, an unwritable path, or a directory instead of a file. It logs a warning naming the bad path. A relative `app_db_path` is resolved against `Path(__file__).parent`, not the process working directory.
  - Table `meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`.
  - `db.meta_get(key: str, default: str | None = None) -> str | None` and `db.meta_set(key: str, value: str) -> None` (upsert).

- [ ] **Step 1: Write the failing tests:**
  - `test_bad_db_path_falls_back_to_default` monkeypatches `config._data["app_db_path"] = str(tmp_path / "missing" / "x.db")`, clears the override, and monkeypatches `db.DEFAULT_PATH` to `tmp_path / "fallback.db"`. Then `db.get()` works and `fallback.db` exists.
  - `test_relative_db_path_resolves_next_to_the_module`.
  - `test_meta_roundtrip_and_upsert`: set "a"="1", set "a"="2", get "a" == "2", get "b" with default "x" == "x".
- [ ] **Step 2:** `PYTEST tests/test_db.py`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement per Interfaces.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `db.py tests/test_db.py`: "db: fall back to the default path; add meta table".

---

### Task 2: Editor-app config keys

**Files:**
- Modify: `config.example.json`, `config_schema.py`
- Test: existing `tests/test_config_schema.py`

**Interfaces:**
- Produces config keys, all in section "Editor app":
  - `gdrive_client_id` (text)
  - `gdrive_client_secret` (secret)
  - `gdrive_redirect_uri` (text; example value `"https://hub.dualbladex.org/auth/gdrive/callback"`)
  - `gdrive_refresh_token` (secret; written by the connect flow)
  - `editor_google_email` (text; the editor's Google account, which Drive files are shared with)
  - `editor_discord_user_id` (text; used by Plan 3)

- [ ] **Step 1:** Add the six keys to `config.example.json` (empty strings, except the redirect URI example) and to `FIELDS`, with a one-line description each.
- [ ] **Step 2:** `PYTEST tests/test_config_schema.py`. Expected: PASS, since `test_every_example_key_has_metadata` covers the new keys.
- [ ] **Step 3: Commit** both files: "config: editor-app Drive and identity keys".

---

### Task 3: Google Drive client

**Files:**
- Create: `gdrive.py`
- Test: `tests/test_gdrive.py`

**Interfaces:**
- Consumes: config keys from Task 2; `config.save()`.
- Produces in `gdrive.py`:
  - The constants from Global Constraints.
  - `class DriveNotConnected(Exception)`: no refresh token, or Google refused it (HTTP 400/401 from the token endpoint).
  - `class DriveError(Exception)`: any other non-2xx from Drive, carrying `.status: int`.
  - `async access_token(now: float | None = None) -> str`: cached until 60 s before expiry, refreshed under an `asyncio.Lock`. **If the refresh response includes a new `refresh_token`, save it** (`config.set` + `config.save`).
  - `forget_token() -> None`
  - `async about() -> dict`: `{"limit": int | None, "usage": int}` from `GET {API}/about?fields=storageQuota`. Google may omit `limit` for unlimited plans.
  - `async create_folder(name: str, parent_id: str | None) -> str` (folder id)
  - `async start_resumable_upload(name: str, parent_id: str, size: int, mime: str, app_properties: dict[str, str], origin: str | None) -> str`: POST `{UPLOAD_API}?uploadType=resumable` with JSON metadata `{name, parents: [parent_id], appProperties}` and headers `X-Upload-Content-Length`, `X-Upload-Content-Type`, plus `Origin: origin` when given. Returns the `Location` header (the session URL).
  - `async get_file(file_id: str) -> dict`: fields `id,name,size,parents,appProperties,webViewLink`. Size is converted to `int`.
  - `async list_folder(folder_id: str) -> list[dict]`: same fields, `q="'<id>' in parents and trashed=false"`, following `nextPageToken`.
  - `async share_with(file_id: str, email: str, role: str = "reader") -> None`: POST `{API}/files/{id}/permissions?sendNotificationEmail=false`.
  - `async delete_file(file_id: str) -> None`: `DELETE`; a `404` returns normally.
  - `async upload_small_file(path: Path, name: str, parent_id: str, mime: str = "application/octet-stream") -> str`: a resumable session plus a single PUT, returning the file id. Used for backups.
  - Every call sends `Authorization: Bearer <access_token()>`. On a `401` from Drive it calls `forget_token()` and retries once.

- [ ] **Step 1: Write the failing tests** (aioresponses):
  - `test_refresh_caches_token_until_near_expiry`
  - `test_rotated_refresh_token_is_saved`: the refresh response carries `refresh_token: "new"`, so `config._data["gdrive_refresh_token"] == "new"` and `config.save` is called. Monkeypatch `save`.
  - `test_missing_refresh_token_raises_not_connected`
  - `test_refused_refresh_raises_not_connected` (400 `invalid_grant`)
  - `test_delete_404_is_success`
  - `test_delete_500_raises_drive_error_with_status`
  - `test_resumable_upload_returns_location_and_sends_origin`: assert the request carried the `Origin` and `X-Upload-Content-Length` headers.
  - `test_401_from_drive_refreshes_once_and_retries`
  - `test_list_folder_follows_next_page_token`
  - `test_about_handles_missing_limit`
- [ ] **Step 2:** `PYTEST tests/test_gdrive.py`. Expected: FAIL (no module).
- [ ] **Step 3:** Implement per Interfaces. Reuse one helper `_request(method, url, **kw)` for auth, the 401 retry and error mapping. Network timeouts and `aiohttp.ClientError` become `DriveError(status=0)`.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `gdrive.py tests/test_gdrive.py`: "gdrive: Drive client (drive.file)".

---

### Task 4: Drive connect flow and folder layout

**Files:**
- Create: `gdrive_oauth.py`
- Modify: `server.py` (register the two routes next to the Spotify ones)
- Test: `tests/test_gdrive_oauth.py`

**Interfaces:**
- Consumes: `gdrive.create_folder`, `gdrive.share_with`, `gdrive.forget_token`, `db.meta_get/meta_set`.
- Produces:
  - `GET /auth/gdrive/login` → 302 to `AUTH_URL` with `client_id`, `redirect_uri`, `response_type=code`, `scope=SCOPE`, `access_type=offline`, `prompt=consent` and a random `state` (kept in memory, as in `spotify_oauth.py`). It answers `400` JSON naming the missing keys when client id or redirect URI are unset.
  - `GET /auth/gdrive/callback`:
    1. Rejects an unknown `state` (400).
    2. Exchanges the code at `TOKEN_URL` (form body, client id and secret in the body).
    3. Saves `gdrive_refresh_token`, then calls `forget_token()`.
    4. Calls `ensure_layout()`.
    5. Answers 302 to `/admin`.
  - `async ensure_layout() -> dict[str, str]`: creates whatever of these is missing and stores their ids in `meta`:
    - `gdrive_root` ("DualBladeX Editor", no parent)
    - `gdrive_projects` ("Projects")
    - `gdrive_brand_kit` ("Brand kit")
    - `gdrive_backups` ("Backups")
    If `editor_google_email` is set and `meta["gdrive_brand_kit_shared"]` is not `"1"`, it shares the brand kit with the editor (`reader`) and sets the flag. It returns the four ids. It is idempotent: a second call creates nothing.

- [ ] **Step 1: Write the failing tests:**
  - `test_login_redirect_carries_offline_consent_and_scope`
  - `test_login_without_client_id_is_400`
  - `test_callback_rejects_unknown_state`
  - `test_callback_saves_refresh_token_and_builds_layout`: mock the token POST and four folder POSTs, and assert the meta ids are set.
  - `test_ensure_layout_is_idempotent`: a second call makes no HTTP requests.
  - `test_brand_kit_shared_once_when_editor_email_set`
- [ ] **Step 2:** `PYTEST tests/test_gdrive_oauth.py`. Expected: FAIL.
- [ ] **Step 3:** Implement and register the routes. Do **not** add them to `OPEN_PATHS`.
- [ ] **Step 4:** `PYTEST`. Expected: passes. `tests/test_auth.py` is unchanged and green.
- [ ] **Step 5: Commit** `gdrive_oauth.py server.py tests/test_gdrive_oauth.py`: "gdrive: one-time connect and folder layout".

---

### Task 5: Editor store: projects, tapes, iterations, events

**Files:**
- Create: `editor_store.py`
- Test: `tests/test_editor_store.py`

**Interfaces:**
- Produces tables, all `created_at`/`updated_at REAL NOT NULL`:
  - `projects(id INTEGER PRIMARY KEY, name TEXT NOT NULL, stream_date TEXT NOT NULL, stream_title TEXT NOT NULL DEFAULT '', vod_filename TEXT NOT NULL, vod_size INTEGER, vod_file_id TEXT, vod_status TEXT NOT NULL CHECK (vod_status IN ('uploading','ready','deleting','deleted')), vod_upload_url TEXT, drive_folder_id TEXT, download_clicked_at REAL, deleted_at REAL, ...)`
  - `tapes(id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), name TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('cutting','in_review','changes_asked','final')) DEFAULT 'cutting', notes TEXT NOT NULL DEFAULT '', drive_folder_id TEXT, files_deleted_at REAL, ...)`
  - `iterations(id INTEGER PRIMARY KEY, tape_id INTEGER NOT NULL REFERENCES tapes(id), version INTEGER NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, status TEXT NOT NULL CHECK (status IN ('uploading','ready','deleting','deleted','abandoned')), drive_file_id TEXT, upload_url TEXT, checks_json TEXT, thumbnail BLOB, uploaded_at REAL, ..., UNIQUE (tape_id, version))`
  - `events(id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL, project_id INTEGER, tape_id INTEGER, iteration_id INTEGER, comment_id INTEGER, summary TEXT NOT NULL)`
- Produces functions. Rows are returned as `dict`, and missing rows give `None`:
  - `create_project(name, stream_date, stream_title, vod_filename, vod_size, now=None) -> dict` (vod_status `uploading`)
  - `get_project(id)`, `list_projects() -> list[dict]` (newest first, excluding `deleted_at` rows, each with `tape_count`)
  - `rename_project(id, name, now=None)`
  - `set_project_folder(id, folder_id)`, `set_vod_upload(id, upload_url)`
  - `mark_vod_ready(id, file_id, now=None) -> dict`
  - `record_download_click(id, now=None) -> dict` (keeps the FIRST click time)
  - `create_tape(project_id, name, now=None) -> dict`, `get_tape(id)`, `list_tapes(project_id)`
  - `update_tape(id, *, name=None, notes=None, status=None, now=None) -> dict`: raises `ValueError` on an unknown status
  - `set_tape_folder(id, folder_id)`
  - `next_version(tape_id) -> int`
  - `create_iteration(tape_id, filename, size, upload_url, now=None) -> dict`
  - `get_iteration(id)`, `list_iterations(tape_id)` (version ascending; omits the thumbnail blob)
  - `complete_iteration(id, file_id, now=None) -> tuple[dict, bool]`: the bool is `True` only on the transition `uploading` → `ready`. It also moves a tape in `cutting` or `changes_asked` to `in_review`.
  - `set_iteration_media(id, *, checks_json=None, thumbnail=None)`: raises `ValueError` over the size limits
  - `previous_ready_iteration(tape_id, before_version) -> dict | None`
  - `record_event(actor, kind, summary, *, project_id=None, tape_id=None, iteration_id=None, comment_id=None, now=None) -> int`
  - `events_since(after_id: int, exclude_actor: str | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing tests:**
  - `test_project_lifecycle_and_rename`
  - `test_download_click_keeps_first_time`
  - `test_versions_increase_per_tape`
  - `test_complete_iteration_is_idempotent` (the second call returns `changed=False`)
  - `test_complete_moves_tape_to_in_review`
  - `test_unknown_status_rejected`
  - `test_thumbnail_over_limit_rejected`
  - `test_events_since_excludes_actor`
- [ ] **Step 2:** `PYTEST tests/test_editor_store.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. One `_DDL` string, `db.ensure(_DDL)` at the top of each public function (Plan 1's `ensure` runs it once). Multi-statement changes use `with conn:` and contain no awaits.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `editor_store.py tests/test_editor_store.py`: "editor: projects, tapes, iterations, events store".

---

### Task 6: Comments, threads, carry-over, search

**Files:**
- Create: `editor_comments.py`
- Test: `tests/test_editor_comments.py`

**Interfaces:**
- Consumes: `editor_store` (`get_iteration`, `previous_ready_iteration`, `record_event`).
- Produces tables:
  - `comments(id INTEGER PRIMARY KEY, iteration_id INTEGER NOT NULL REFERENCES iterations(id), author TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('fix','keep','question')), start_s REAL, end_s REAL, body TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL)`: `start_s` NULL means a general comment; `end_s` NULL means a point comment
  - `replies(id INTEGER PRIMARY KEY, comment_id INTEGER NOT NULL REFERENCES comments(id), author TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL)`
  - `comment_state(comment_id INTEGER NOT NULL, iteration_id INTEGER NOT NULL, resolved INTEGER NOT NULL DEFAULT 0, resolved_by TEXT, resolved_at REAL, PRIMARY KEY (comment_id, iteration_id))`
- Produces functions:
  - `add_comment(iteration_id, author, kind, body, start_s=None, end_s=None, now=None) -> dict`. It validates the kind, a non-empty body, `start_s >= 0`, and that `end_s` is either None or `> start_s` with `start_s` set. It inserts the comment plus its `comment_state` row for its own iteration, and records an event `comment_added`.
  - `edit_comment(id, author, *, body=None, kind=None, now=None) -> dict`: raises `PermissionError` if `author` is not the comment's author
  - `add_reply(comment_id, author, body, now=None) -> dict`, which records an event `reply_added`
  - `set_resolved(comment_id, iteration_id, resolved: bool, by: str, now=None) -> dict`, which records an event `comment_resolved` when resolved
  - `comments_for_iteration(iteration_id) -> list[dict]`. Each dict has the comment fields plus:
    - `origin_iteration_id`, `origin_version`
    - `carried: bool` (True when the origin is an earlier iteration)
    - `resolved`
    - `replies: list[dict]`
    For carried comments `start_s`/`end_s` are still returned, so the UI can say "from v1 at 03:42", but the UI must not place them on this iteration's scrub bar. That is Plan 5's job; here they are only flagged `carried`.
  - `carry_over(iteration_id, now=None) -> int`: for the tape's previous ready iteration, every comment whose `comment_state` on that iteration is unresolved gets a `comment_state` row on `iteration_id` (INSERT OR IGNORE). It returns the count of new rows, so it is idempotent.
  - `search(query: str, limit: int = 50) -> list[dict]`: case-insensitive substring match over comment and reply bodies. Each hit is `{kind: "comment"|"reply", comment_id, iteration_id, tape_id, project_id, version, snippet, start_s}`, newest first. An empty or whitespace query returns `[]`. LIKE wildcards in the query are escaped.

- [ ] **Step 1: Write the failing tests:**
  - `test_general_point_and_range_comments_validate`
  - `test_only_author_can_edit`
  - `test_reply_thread_order`
  - `test_carry_over_takes_unresolved_only`: on v1, A is unresolved and B is resolved; after carry-over to v2, v2 lists A as `carried=True, resolved=False` and does not list B; v1 still lists both.
  - `test_carry_over_is_idempotent`
  - `test_resolving_on_v2_does_not_change_v1`
  - `test_carry_chain_v1_to_v3`: a comment unresolved through v2 appears on v3 with `origin_version == 1`
  - `test_search_finds_reply_text_and_escapes_wildcards` (searching `"%"` must not match everything)
- [ ] **Step 2:** `PYTEST tests/test_editor_comments.py`. Expected: FAIL.
- [ ] **Step 3:** Implement per Interfaces.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `editor_comments.py tests/test_editor_comments.py`: "editor: comments, threads, carry-over, search".

---

### Task 7: Scheduler with a clock-sanity gate

**Files:**
- Create: `scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `db.meta_get/meta_set`.
- Produces:
  - `TICK_SECONDS = 30`
  - `backoff_seconds(attempts: int) -> float` = `min(30 * 2**attempts, 3600)`
  - `register(name: str, job: Callable[[float], Awaitable[None]]) -> None`: jobs run in registration order
  - `clock_is_sane(now: float) -> bool`: false when `now < float(meta["clock_last_seen"])`, true when the key is absent
  - `async tick(now: float | None = None) -> bool`. If the clock is not sane it logs once per insane streak and returns `False`. Otherwise it runs every job with `now`; an exception in one job is logged and does not stop the others. It sets `meta["clock_last_seen"] = str(now)` after the jobs, and returns `True`.
  - `async start() -> None` / `async stop() -> None`: a background task calling `tick()` every `TICK_SECONDS`, as `health_checks.start()` does.
  - `boot_time() -> float`: the time `start()` was first called in this process. Task 8 uses it.

- [ ] **Step 1: Write the failing tests:**
  - `test_backoff_caps_at_an_hour`
  - `test_clock_behind_last_seen_blocks_jobs`: tick at 1000, then a tick at 900 runs no job and returns False; a tick at 1001 runs again
  - `test_one_failing_job_does_not_stop_others`
  - `test_last_seen_persists_across_reconnect`: `db.use_path_for_tests(same path)`
- [ ] **Step 2:** `PYTEST tests/test_scheduler.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `scheduler.py tests/test_scheduler.py`: "scheduler: absolute-time job loop with clock-sanity gate".

---

### Task 8: Two-step deletes with one-hour undo

**Files:**
- Create: `deletions.py`
- Test: `tests/test_deletions.py`

**Interfaces:**
- Consumes:
  - `editor_store`: `get_project`, `get_tape`, `list_iterations`, `record_event`, and direct updates to `vod_status`/`deleted_at`/`files_deleted_at`/iteration `status`
  - `gdrive.delete_file`, `gdrive.DriveNotConnected`, `gdrive.DriveError`
  - `scheduler.backoff_seconds`, `scheduler.boot_time`
- Produces tables:
  - `delete_requests(id INTEGER PRIMARY KEY, kind TEXT NOT NULL CHECK (kind IN ('vod','tape_files','project')), target_id INTEGER NOT NULL, label TEXT NOT NULL, requested_by TEXT NOT NULL, requested_at REAL NOT NULL, delete_at REAL NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','running','done','cancelled')))`
  - `delete_files(id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES delete_requests(id), drive_file_id TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('pending','deleting','deleted')), prev_status TEXT, target_kind TEXT NOT NULL CHECK (target_kind IN ('vod','iteration','folder')), target_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL, last_error TEXT)` (`prev_status` = the VOD/iteration status before the request, restored by undo)
- Produces functions:
  - `DELETE_GRACE_SECONDS = 3600`
  - `class ConfirmMismatch(ValueError)`, `class NotAllowed(PermissionError)`
  - `request_delete(kind, target_id, confirm: str, *, username: str, role: str, now=None) -> dict`
    - **Two-step rule:** `confirm` must equal, after `strip()` and exactly otherwise: the project's `vod_filename` for `vod`, the tape name for `tape_files`, the project name for `project`. Otherwise it raises `ConfirmMismatch`.
    - **Permissions:** an editor may request only `vod`, and only once the download was clicked. Everything else needs `admin`; otherwise it raises `NotAllowed`.
    - It collects the Drive ids:
      - `vod`: the VOD file
      - `tape_files`: every iteration with a `drive_file_id` whose status is not `deleted`
      - `project`: the project's `drive_folder_id`; deleting the folder deletes its children
    - It inserts the request with `delete_at = now + 3600` and marks targets `deleting` where a status exists (the VOD, the iterations).
    - It records an event `delete_scheduled` and returns `{request_id, delete_at, label}`.
  - `undo(request_id, *, username, role, now=None) -> dict`: only while `pending`, by an admin or by the requester. It sets `cancelled`, restores target statuses to what they were (from each file row's `prev_status`), and records an event `delete_undone`.
  - `pending() -> list[dict]`: pending and running requests with their remaining seconds
  - `apply_boot_grace(boot: float) -> int`: `UPDATE ... SET delete_at = max(delete_at, boot + 3600) WHERE status='pending'`, and resets `deleting` file rows to `pending`. Returns the count of requests touched.
  - `async run_due(now: float) -> None`, registered as a scheduler job:
    1. Takes pending requests with `delete_at <= now` and sets them `running`.
    2. For each file row that is `pending` with `next_attempt_at <= now`: set `deleting` and commit, then `await gdrive.delete_file`. On success set `deleted`. On `DriveNotConnected` or `DriveError`, set it back to `pending` with `attempts+1`, `next_attempt_at = now + backoff`, `last_error`, and **never** `deleted`.
    3. When all of a request's files are `deleted`: set the request `done` and finalize the targets: the VOD gets `vod_status='deleted'`; iterations get `status='deleted'` and the tape gets `files_deleted_at`; a project gets `deleted_at`. Record an event `delete_done`.

- [ ] **Step 1: Write the failing tests** (monkeypatch `gdrive.delete_file`):
  - `test_confirm_must_match_name_exactly`
  - `test_editor_may_only_delete_downloaded_vod`
  - `test_editor_cannot_delete_tape_files_or_project`
  - `test_nothing_deleted_before_the_hour`: `run_due(now+3599)` makes no calls; `run_due(now+3600)` deletes
  - `test_undo_restores_statuses_and_blocks_deletion`
  - `test_editor_cannot_undo_admins_request`
  - `test_boot_grace_gives_a_full_hour_after_restart`: a request due 10 min before boot becomes due at boot+3600
  - `test_retry_after_crash_treats_404_as_done`: a row left `deleting`, then `apply_boot_grace`, then `run_due` with `gdrive.delete_file` succeeding (404 is success inside gdrive), gives `deleted` and the request `done`
  - `test_drive_not_connected_keeps_pending_with_backoff`: attempts goes 1→2, `next_attempt_at` grows, the status is never `deleted`
  - `test_tape_files_marks_iterations_deleted_and_keeps_tape`
- [ ] **Step 2:** `PYTEST tests/test_deletions.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. Commit the `deleting` mark **before** the await, and write the outcome after it. Never hold a transaction across the await.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `deletions.py tests/test_deletions.py`: "deletions: two-step requests, one-hour undo, idempotent executor".

---

### Task 9: Drive-backed editor flows

**Files:**
- Create: `editor_service.py`
- Test: `tests/test_editor_service.py`

**Interfaces:**
- Consumes: `editor_store`, `editor_comments.carry_over`, `gdrive` (all of Task 3), `gdrive_oauth.ensure_layout`, `db.meta_get`.
- Produces:
  - `class DriveFull(Exception)` with `.needed: int` and `.free: int`
  - `async free_space() -> dict`: `{"limit", "usage", "free"}`. `free` is `None` when `limit` is `None`.
  - `async ensure_project_folder(project_id) -> str` and `async ensure_tape_folder(tape_id) -> str`. They create the folder under `gdrive_projects` or the project folder on first use, store the id, and are idempotent.
  - `async start_iteration_upload(tape_id, filename, size, mime) -> dict`:
    1. Refuses with `DriveFull` when `free` is not None and `size > free`.
    2. Creates the iteration row (version = `next_version`).
    3. Starts a resumable upload in the tape folder, named `f"v{version} - {filename}"` with `appProperties {"iteration_id": str(id)}` and `origin=config.get("public_base_url") or None`.
    4. Stores the URL and returns `{iteration_id, version, upload_url}`.
  - `async complete_iteration_upload(iteration_id, file_id, actor) -> dict`:
    1. `gdrive.get_file(file_id)` must have the tape folder in `parents` and `appProperties.iteration_id == str(iteration_id)`; otherwise raise `ValueError("That file is not this iteration's upload")`.
    2. Calls `editor_store.complete_iteration`.
    3. Only when `changed`: runs `carry_over` and records an event `iteration_ready` ("{tape} v{n} is ready").
    4. Returns the iteration.
  - `async vod_ready(project_id, file_id, actor) -> dict`: marks the VOD ready, shares it with `editor_google_email` (reader) when set, and records an event `vod_ready`. Plan 4's uploader route calls this; Plan 3's DM listens for the event.
  - `async media_link(iteration_id: int | None = None, project_id: int | None = None) -> dict`: `{"url": f"{API}/files/{id}?alt=media", "access_token": <token>, "expires_in": <seconds>, "drive_url": webViewLink}`. Plan 5's player and download buttons use it. This deliberately hands the browser a short-lived `drive.file` token: it can only reach files this app created, which both users can already open. Put a `ponytail:` comment naming that ceiling.
  - `async storage_view() -> dict`: `{"quota": free_space(), "files": [{kind: "vod"|"iteration", label, size, project_id, tape_id, status}] (status ready/deleting only), "pending_deletes": deletions.pending()}`

- [ ] **Step 1: Write the failing tests** (monkeypatch `gdrive` functions; no HTTP):
  - `test_start_upload_refuses_when_drive_too_full` (message values via `.needed`/`.free`)
  - `test_start_upload_names_file_and_sets_app_properties`
  - `test_complete_rejects_a_foreign_file`
  - `test_complete_twice_carries_over_once_and_logs_one_event`
  - `test_vod_ready_shares_with_editor_email_when_set`
  - `test_folders_created_once`
- [ ] **Step 2:** `PYTEST tests/test_editor_service.py`. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** `editor_service.py tests/test_editor_service.py`: "editor: Drive-backed upload, share, media and storage flows".

---

### Task 10: Boot reconciliation and nightly backup

**Files:**
- Create: `reconcile.py`, `backup.py`
- Test: `tests/test_reconcile.py`, `tests/test_backup.py`

**Interfaces:**
- Consumes: `editor_store`, `editor_service.complete_iteration_upload`, `gdrive.list_folder`, `gdrive.upload_small_file`, `gdrive.delete_file`, `db`, `scheduler`.
- Produces:
  - `reconcile.ABANDON_AFTER_SECONDS = 8 * 86400`
  - `async reconcile.run_once(now: float | None = None) -> dict`: `{"completed": n, "abandoned": m}`.
    1. For every iteration in `uploading`, list its tape folder. A file whose `appProperties.iteration_id` matches is completed via `complete_iteration_upload(actor="system")`.
    2. An `uploading` iteration older than `ABANDON_AFTER_SECONDS` with no file becomes `abandoned`, but only when `scheduler.clock_is_sane(now)`, so a clock booted into the future can't abandon live uploads (Drive keeps no partial file, so nothing needs deleting).
    3. Unknown files in app folders are logged, never deleted.
    4. Drive errors are logged and the function returns what was done. It never raises.
  - `backup.BACKUP_HOUR = 4`, `backup.BACKUP_KEEP = 7`
  - `async backup.run_if_due(now: float) -> None`, a scheduler job:
    1. Only when local time is past 04:00 and `meta["backup_last_date"]` is not today's local date.
    2. Copies `app.db` with `sqlite3.Connection.backup()` into a temp file.
    3. Uploads it to `gdrive_backups` as `app-YYYY-MM-DD.db`.
    4. Deletes the oldest backups beyond 7 (list the folder, sort by name). These are app-internal backups, so they skip the two-step rule; say so in a comment.
    5. Sets `backup_last_date` **only after** the upload succeeded.

- [ ] **Step 1: Write the failing tests:**
  - `test_reconcile_completes_upload_found_in_folder`
  - `test_reconcile_abandons_stale_upload`
  - `test_reconcile_never_deletes_unknown_files`
  - `test_reconcile_survives_drive_error`
  - `test_backup_runs_once_per_day_after_4am` (inject `now` for 03:59 and 04:01 local)
  - `test_backup_failure_retries_next_tick` (`backup_last_date` stays unset)
  - `test_backup_keeps_newest_seven`
- [ ] **Step 2:** `PYTEST tests/test_reconcile.py tests/test_backup.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. Compute local dates with `time.localtime(now)`.
- [ ] **Step 4:** `PYTEST`. Expected: passes.
- [ ] **Step 5: Commit** the four files: "editor: boot reconciliation and nightly DB backup".

---

### Task 11: Personal data and the `/api/editor` routes

**Files:**
- Create: `editor_personal.py`, `editor_api.py`
- Modify: `server.py` (call `editor_api.register_routes(app)` before `site_server`), `main.py` (startup wiring)
- Test: `tests/test_editor_personal.py`, `tests/test_editor_api.py`

**Interfaces:**
- Consumes: everything above, plus `auth.session_for(request)` → `{"username","role"}`.
- Produces in `editor_personal.py`. Tables are keyed by username, and other users' rows are invisible (missing rows return `None`, so handlers answer 404):
  - `todos(id INTEGER PRIMARY KEY, owner TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL)`: `list_todos(owner)`, `add_todo(owner, text)`, `update_todo(owner, id, *, text=None, done=None)`, `delete_todo(owner, id) -> bool`
  - `tape_seen(username TEXT, tape_id INTEGER, seen_at REAL, PRIMARY KEY (username, tape_id))`: `mark_tape_seen(username, tape_id, now=None)`, `tape_has_new(username, tape_id) -> bool` (true when an event on that tape by someone else is newer than `seen_at`; never seen counts as new if any such event exists)
  - `playback(username TEXT, iteration_id INTEGER, position_s REAL, updated_at REAL, PRIMARY KEY (username, iteration_id))`: `set_position`, `get_position -> float` (0.0 when none)
- Produces routes in `editor_api.py`. Every handler reads `auth.session_for`. Bodies are JSON. Errors are JSON `{"error": ...}`. The "admin" column means role-checked inside, giving 403 `{"error": "Only the streamer can do this."}` otherwise.

| Method & path | Who | Does |
|---|---|---|
| GET `/api/editor/me` | both | `{username, role}` |
| GET `/api/editor/projects` | both | `list_projects` + per-project `has_new` (any tape) |
| GET `/api/editor/projects/{id}` | both | project + `list_tapes` (each with `has_new`, latest version, open-comment count) |
| PATCH `/api/editor/projects/{id}` | admin | `{name}` rename |
| POST `/api/editor/projects/{id}/download` | both | `record_download_click`; returns `media_link(project_id=)` |
| POST `/api/editor/projects/{id}/tapes` | both | `{name}` create tape |
| GET `/api/editor/tapes/{id}` | both | tape + iterations (no blobs) + `mark_tape_seen` |
| PATCH `/api/editor/tapes/{id}` | both; `status:"final"` admin | `{name?, notes?, status?}` |
| POST `/api/editor/tapes/{id}/iterations` | both | `{filename, size, mime}` → `start_iteration_upload`; `DriveFull` → **507** `{"error": "Drive is too full for this file", "needed", "free"}` |
| POST `/api/editor/iterations/{id}/complete` | both | `{file_id}` → `complete_iteration_upload` |
| GET `/api/editor/iterations/{id}` | both | iteration + `comments_for_iteration` + my `position_s` |
| GET `/api/editor/iterations/{id}/thumbnail` | both | `image/jpeg` bytes or 404 |
| PUT `/api/editor/iterations/{id}/media` | both | `{checks?, thumbnail_b64?}`, over-limit → 413 |
| GET `/api/editor/iterations/{id}/media-link` | both | `media_link(iteration_id=)` |
| PUT `/api/editor/iterations/{id}/position` | both | `{position_s}` |
| POST `/api/editor/iterations/{id}/comments` | both | `{kind, body, start_s?, end_s?}` |
| PATCH `/api/editor/comments/{id}` | author | `{body?, kind?}`; not author → 403 |
| POST `/api/editor/comments/{id}/replies` | both | `{body}` |
| PUT `/api/editor/iterations/{iid}/comments/{cid}/resolved` | admin | `{resolved}` |
| GET `/api/editor/search?q=` | both | `search` |
| GET/POST `/api/editor/todos`, PATCH/DELETE `/api/editor/todos/{id}` | owner | personal to-dos |
| POST `/api/editor/deletions` | per rules | `{kind, target_id, confirm}` → 202; `ConfirmMismatch` → 400 `{"error": "Type the name exactly to confirm"}`; `NotAllowed` → 403 |
| POST `/api/editor/deletions/{id}/undo` | per rules | → 200 |
| GET `/api/editor/deletions` | both | `pending()` |
| GET `/api/editor/storage` | admin | `storage_view()` |
| GET `/api/editor/drive/status` | admin | `{connected: bool, layout: bool, quota?}`; never raises; `DriveNotConnected` → `connected: false` |

- Startup wiring in `main.py`, after `health_checks.start()`:
  1. `deletions.apply_boot_grace(time.time())`
  2. `scheduler.register("deletions", deletions.run_due)`
  3. `scheduler.register("backup", backup.run_if_due)`
  4. `await scheduler.start()`
  5. `asyncio.create_task(reconcile.run_once())`. It is a one-shot; its abandon step already checks the clock (Task 10).
  6. On shutdown, `await scheduler.stop()`.

- [ ] **Step 1: Write the failing tests:**
  - `test_todos_are_private_per_owner` (personal)
  - `test_tape_has_new_ignores_own_events` (personal)
  - `test_position_roundtrip` (personal)
  - `test_editor_is_refused_admin_actions` (API, using `aiohttp.test_utils` with `auth.auth_middleware`, real `create_session` cookies for an admin `aryan` and an editor `cutguy` configured via `config._data`). Each of these with the editor cookie → 403, and the action not done:
    - rename project
    - status final
    - resolve
    - delete `tape_files`
    - storage
    - drive status
    - undo of an admin request
  - `test_admin_can_do_admin_actions`
  - `test_drive_full_gives_507_with_numbers`
  - `test_confirm_mismatch_gives_400`
  - `test_other_users_todo_is_404`
  - `test_iteration_payload_includes_carried_comments_and_position`
- [ ] **Step 2:** `PYTEST tests/test_editor_personal.py tests/test_editor_api.py`. Expected: FAIL.
- [ ] **Step 3:** Implement. Keep handlers thin: they parse, call one module function and map exceptions. Map `gdrive.DriveNotConnected` to 503 `{"error": "Google Drive isn't connected - connect it from the dashboard."}` everywhere.
- [ ] **Step 4:** `PYTEST`. Expected: passes, and `tests/test_auth.py` is unchanged and green.
- [ ] **Step 5: Commit** the six files: "editor: personal data and /api/editor routes, wired at startup".

---

### Task 12: Setup guide, docs, roadmap

**Files:**
- Create: `GDRIVE_SETUP.md`
- Modify: `/home/aryan/projects/CLAUDE.md` (this file is local and outside the repo, so it is not committed), `/home/aryan/projects/docs/superpowers/plans/2026-10-25-editor-app-roadmap.md`

- [ ] **Step 1:** Write `GDRIVE_SETUP.md`: the streamer's one-time Google setup, as numbered steps.
  1. Create a Google Cloud project.
  2. Enable the Drive API.
  3. Set up the OAuth consent screen: External, the scope `drive.file` only, then **Publish app** to production. Explain why: Testing mode expires refresh tokens after 7 days, and `drive.file` needs no verification.
  4. Create an OAuth client of type "Web application" with the authorized redirect URI `https://hub.dualbladex.org/auth/gdrive/callback`.
  5. Paste the client id, the secret and the redirect URI into the Settings page under "Editor app", along with the editor's Google email.
  6. Open `https://hub.dualbladex.org/auth/gdrive/login` while logged in as admin, and approve with the Google account that has the Google One plan.
  7. Confirm in Drive that the "DualBladeX Editor" folder appeared.
- [ ] **Step 2:** In CLAUDE.md, add a short architecture subsection, "The editor backend", covering:
  - the modules and their one-line roles
  - large files never pass through the Mac Mini
  - every time is absolute in SQLite, behind the scheduler's clock gate
  - deletes are two-step, permanent, idempotent, and get the boot grace
  - admin-only checks happen inside the `/api/editor` handlers
  - the `media-link` token ceiling
  - the Drive constants
- [ ] **Step 3:** Roadmap:
  - tick each Plan-2 row this plan delivered
  - set Plan 2's status to "built, not yet shipped"
  - fill "### Plan 2" under "What each plan hands on" with the exact module, function, table, route and config-key names. Plan 3 needs `events`/`events_since`, `vod_ready`, and the `download_clicked_at` + `delete_requests` fields for its reminders. Plan 4 needs `create_project`, `ensure_project_folder`, `set_vod_upload`, `vod_ready` and `free_space`, plus `gdrive.start_resumable_upload`.
- [ ] **Step 4:** `PYTEST` one final time and record the summary line.
- [ ] **Step 5: Commit** `GDRIVE_SETUP.md`: "docs: Google Drive setup guide". The roadmap and CLAUDE.md are local files outside the repo.

---

### Task 13: Clips, reels, combine, start over (added 2026-10-27)

Spec section "Clips and reels". Builds on Tasks 5, 6, 8, 9 and 11 as they now stand. Read their current code; the final fix wave changed signatures (for example `editor_service.create_tape` and `complete_iteration(..., actor=)`).

**Files:**
- Modify: `editor_store.py`, `editor_comments.py`, `editor_service.py`, `deletions.py`, `editor_api.py`
- Test: the matching `tests/test_*.py`

**Interfaces:**
- Schema:
  - `projects.kind TEXT NOT NULL DEFAULT 'vod' CHECK (kind IN ('vod','clip'))`, plus `projects.duration_s REAL`
  - `tapes.kind TEXT NOT NULL DEFAULT 'tape' CHECK (kind IN ('tape','reel'))`
  - new table `reel_sources(tape_id INTEGER NOT NULL REFERENCES tapes(id), project_id INTEGER NOT NULL UNIQUE REFERENCES projects(id), PRIMARY KEY (tape_id, project_id))`. The UNIQUE on `project_id` enforces "a clip is used in at most one reel".
  - The DB is not deployed yet, so change the DDL in place; no migration.
- `editor_store.CLIP_MAX_SECONDS = 300`
- `create_project(..., duration_s: float | None = None)`: `kind = 'clip'` when `duration_s is not None and duration_s < 300`, else `'vod'`. A clip gets its reel in the same `db.transaction()`: a tape named after the clip name, `kind='reel'`, plus its `reel_sources` row.
- `create_tape` on a clip project raises `ValueError("A clip has exactly one reel")`.
- `reel_for_clip(project_id) -> dict | None` returns the reel tape that the clip is a source of.
- `get_project` for a clip includes `reel_tape_id`. `get_tape` for a reel includes `source_project_ids` (oldest first).
- `combine_clips(project_ids: list[int], name: str, actor: str, now=None) -> dict` (in `editor_service`):
  - needs ≥2 distinct ids, all `kind='clip'`, not deleted, and none of their reels has any iteration (otherwise `ValueError`)
  - one transaction: keep the first clip's reel and rename it to `name`, point every listed clip's `reel_sources` row at it, delete the other (empty) reel tapes, and record the event `reels_combined`
  - returns the reel
- `uncombine(tape_id, actor) -> list[dict]`: only for a reel with ≥2 sources and no iterations. Each extra source gets back its own reel named after the clip. Records the event `reels_uncombined`.
- `editor_comments.start_over(iteration_id, by, now=None) -> int`: resolves every unresolved `comment_state` row on that iteration, records one event `start_over`, and returns the count.
- `deletions.request_delete("project", ...)` on a clip whose project hosts a reel with ≥2 sources raises `ValueError("This clip holds a combined reel - delete the reel's files first")`, unless that reel's files are already deleted.
- API (`editor_api.py`):
  - `POST /api/editor/clips/combine` `{project_ids, name}`: both roles
  - `POST /api/editor/tapes/{id}/uncombine`: both roles
  - `POST /api/editor/iterations/{id}/start-over`: admin only
  - project payloads include `kind`, `duration_s`, `reel_tape_id`; tape payloads include `kind`, `source_project_ids`
  - `POST /api/editor/projects/{id}/tapes` on a clip → 400 with the message above

- [ ] **Step 1: Write the failing tests:**
  - `test_short_upload_becomes_a_clip_with_one_reel`: `duration_s=299` gives a clip with a reel; 300 and None give a VOD with no tape
  - `test_second_reel_on_a_clip_is_refused` (store and API → 400)
  - `test_combine_two_clips_replaces_their_reels`
  - `test_combine_refused_when_a_reel_has_a_version`
  - `test_a_clip_cannot_be_in_two_reels`
  - `test_uncombine_restores_own_reels`
  - `test_uncombine_refused_after_a_version`
  - `test_start_over_resolves_all_open_comments_on_that_version_only` (v1 untouched when run on v2)
  - `test_editor_cannot_start_over` (API 403)
  - `test_project_delete_refused_for_clip_holding_combined_reel`
  - `test_reel_works_like_a_tape`: upload → complete → comments carry over to v2
- [ ] **Step 2:** `PYTEST` on those files. Expected: FAIL.
- [ ] **Step 3:** Implement. Multi-statement writes go in `db.transaction()`, with no await inside.
- [ ] **Step 4:** Full `PYTEST`. Expected: only the 12 tesseract failures.
- [ ] **Step 5:** Update the roadmap's "### Plan 2" hand-on with the new names, tick the Plan 2 "Clips and reels" rows, and add one paragraph to CLAUDE.md's editor-backend subsection. Commit (stage by name): "editor: clips with a single reel, combine/uncombine, start over".
