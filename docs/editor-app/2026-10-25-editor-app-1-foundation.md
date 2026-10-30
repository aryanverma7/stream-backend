# Editor App — Plan 1 of 5: Foundation (security, logins with roles, settings page)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing backend and admin site safe to give a second person a login: patched dependencies, power-safe config writes, persistent expiring sessions with admin/editor roles, secrets never sent to the browser, and a structured settings page replacing the raw-JSON config editor.

**Architecture:** Backend changes stay in `mac mini/Stream` (aiohttp, one process). A new stdlib-`sqlite3` module `db.py` becomes the app database later plans build on; sessions are its first table. A new `config_schema.py` is the single source of setting metadata, served to the frontend at `GET /api/config/schema`. The frontend rewrite keeps `components/admin/ConfigEditor.tsx` as the entry point so `AdminDashboard.tsx` is untouched apart from the tab hint.

**Tech Stack:** Python 3.14, aiohttp 3.14.5, sqlite3 (stdlib), pytest + pytest-asyncio + aioresponses; Next.js 16 static export, React 19, Vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md` (sections "Roles and login", "Also in this build: settings page and security fixes", "Power cuts").

**Roadmap (later plans, written after this one ships):** 2 Editor backend (data model, Drive, deletion scheduler, reconciliation, backup) · 3 Discord DMs and notifications · 4 Gaming PC uploader · 5 Editor frontend (navigation, review player, checks).

## Global Constraints

- Backend tests run in WSL via uv (pip is absent): from `mac mini/Stream`,
  `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings`
  — referred to below as **`PYTEST`** (append a test path to narrow it). Baseline before Task 1: 539 passed, 12 failed, 14 skipped; the 12 failures are all in `tests/test_credit_ocr.py` and need the `tesseract` binary, which this machine lacks. "Passes" below means no failures outside that file.
- Frontend tests: from `mac mini/Site`, `npx vitest run` (append a path to narrow). Build check: `npx next build`. If either reports a native-binding error, `npm install` first; if Turbopack fails to open its cache, `rm -rf .next out`.
- Pins: `aiohttp==3.14.5`, `Pillow==13.0.0`, `python-socketio==4.6.1` (unchanged: Streamlabs needs Engine.IO v3), `next` ≥ 16.4.1 and `eslint-config-next` at the same version.
- No new Python runtime dependency. SQLite is stdlib `sqlite3`.
- Every new `/api/status`-style field read by the frontend is optional on its TypeScript type (CLAUDE.md rule: the Mac Mini often runs a backend older than the site).
- Async backend tests carry `@pytest.mark.asyncio`, matching `tests/test_auth.py`.
- Timing code takes an injected `now` rather than calling `time.time()` directly in a way tests cannot control.
- The tree is not a git repository (CLAUDE.md "Environment note"). Tasks end with a checkpoint, not a commit; Task 11 ships.
- `SECRET_MASK` is the exact string `"••••••••"` (eight U+2022) on both backend and frontend.
- Session lifetime: 30 days (`2592000` seconds).

## Review Focus

1. **A form save that never touched a secret** must leave the stored secret exactly as it was. The mask must never be written into `config.json`. Pinned in Task 6 (`test_put_with_the_mask_keeps_the_stored_secret`) and Task 8 (`buildPayload omits untouched secrets`).
2. **A logged-in editor calling admin APIs** (`/api/config`, `/api/config/schema`, `/api/points/x`, `/api/logs`) must get a 403 JSON error, never data. Pinned in Task 5.
3. **A power cut during a config save** must leave the previous `config.json` intact and parseable. Pinned in Task 2.
4. **A site deployed ahead of its backend**: `/api/config/schema` returns 404, so the settings page must fall back to the Advanced raw-JSON view with a one-line notice, not render blank or crash. Pinned in Tasks 9 and 10.
5. **Nested values the form has no widget for** (`streamerbot_subscribe_events` is a dict of lists) must render as an editable JSON field whose invalid text blocks Save, not crash the page or be silently flattened. Pinned in Tasks 8 and 10.

---

### Task 1: Upgrade backend dependencies

**Files:**
- Modify: `mac mini/Stream/requirements.txt` (aiohttp, Pillow lines; also replace the stale `brew install tesseract` comment with MacPorts `/opt/local/bin` per CLAUDE.md)
- Modify: `gaming pc/requirements.txt` (`Pillow>=13.0.0`)
- Modify: whatever backend source or tests the upgrade breaks

**Interfaces:** none new.

- [ ] **Step 1:** Change `aiohttp==3.10.11` to `aiohttp==3.14.5` and `Pillow==12.3.0` to `Pillow==13.0.0`.
- [ ] **Step 2:** Run `PYTEST`. Expected: no failures outside `tests/test_credit_ocr.py`. Fix any breakage in source (not by weakening tests). The "Bare functions are deprecated" warning means a sync handler was registered; make it `async def`.
- [ ] **Step 3:** Run `PYTEST` again. Expected: same pass count as the baseline or higher, failures only in `test_credit_ocr.py`.
- [ ] **Step 4: Checkpoint.** Note in the task report which source files changed.

---

### Task 2: Power-safe `config.json` writes

**Files:**
- Modify: `mac mini/Stream/config.py` (`Config.save`)
- Test: `mac mini/Stream/tests/test_config.py`

**Interfaces:**
- Produces: `Config.save() -> None`, now atomic: writes `<path>.tmp` in the same directory, `flush()` + `os.fsync()`, then `os.replace()` onto the real path.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_replaces_the_file_atomically(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}')
    c = Config(path)
    c.set("a", 2)
    c.save()
    assert json.loads(path.read_text()) == {"a": 2}
    assert not (tmp_path / "config.json.tmp").exists()

def test_a_crash_mid_save_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}')
    c = Config(path)
    c.set("a", 2)
    def boom(*args, **kwargs):
        raise OSError("power cut")
    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(OSError):
        c.save()
    assert json.loads(path.read_text()) == {"a": 1}
```

- [ ] **Step 2:** Run `PYTEST tests/test_config.py`. Expected: the second test FAILS (current `save` truncates the file before dumping).
- [ ] **Step 3:** Implement the atomic `save()` described in Interfaces. Remove a leftover `.tmp` if the dump raises, then re-raise.
- [ ] **Step 4:** Run `PYTEST tests/test_config.py`. Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 3: App database module

**Files:**
- Create: `mac mini/Stream/db.py`
- Modify: `mac mini/Stream/config.example.json` (add `"app_db_path": ""`)
- Test: `mac mini/Stream/tests/test_db.py`

**Interfaces:**
- Produces:
  - `db.DEFAULT_PATH: Path` = `Path(__file__).parent / "app.db"`
  - `db.get() -> sqlite3.Connection`: process-wide connection, opened lazily at `config.get("app_db_path") or DEFAULT_PATH`, with `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=FULL`, `PRAGMA foreign_keys=ON`, `row_factory = sqlite3.Row`, `isolation_level=None` (autocommit; callers use `with conn:` blocks or explicit `BEGIN`).
  - `db.ensure(ddl: str) -> None`: `executescript` of idempotent `CREATE TABLE IF NOT EXISTS ...` statements; each module calls it once for its own tables.
  - `db.use_path_for_tests(path: Path) -> None`: closes any open connection and makes the next `get()` open `path`.
- Also add `app.db`, `app.db-wal`, `app.db-shm` to the backend repo's `.gitignore` in Task 11's clone.

- [ ] **Step 1: Write the failing tests** in `tests/test_db.py`: `test_get_uses_wal_and_full_sync` (asserts `PRAGMA journal_mode` returns `"wal"` and `PRAGMA synchronous` returns `2`), `test_ensure_is_idempotent` (calling `ensure` with the same `CREATE TABLE IF NOT EXISTS t(x)` twice raises nothing and the table exists), `test_rows_are_addressable_by_name` (`get().execute("select 1 as one").fetchone()["one"] == 1`). Each test calls `db.use_path_for_tests(tmp_path / "t.db")`.
- [ ] **Step 2:** Run `PYTEST tests/test_db.py`. Expected: FAIL, `No module named 'db'`.
- [ ] **Step 3:** Implement `db.py` per Interfaces.
- [ ] **Step 4:** Run `PYTEST tests/test_db.py`. Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 4: Persistent, expiring sessions; logout; Secure cookie

**Files:**
- Modify: `mac mini/Stream/auth.py`
- Modify: `mac mini/Stream/server.py` (register `POST /auth/logout`)
- Test: `mac mini/Stream/tests/test_auth.py` (replace the `_valid_sessions` fixture with a per-test temp database)

**Interfaces:**
- Consumes: `db.get()`, `db.ensure()`, `db.use_path_for_tests()` (Task 3).
- Produces:
  - Table `sessions(token TEXT PRIMARY KEY, username TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('admin','editor')), created_at REAL NOT NULL, expires_at REAL NOT NULL)`.
  - `auth.SESSION_MAX_AGE_SECONDS = 2592000`
  - `auth.create_session(username: str, role: str, now: float | None = None) -> str`
  - `auth.session_for(request: web.Request, now: float | None = None) -> dict | None`, returning `{"username": str, "role": str}` for a live session. An expired row is deleted and `None` returned.
  - `auth.is_authenticated(request) -> bool` stays, as `session_for(request) is not None`.
  - `auth.handle_logout(request) -> web.Response`: deletes the row, expires the cookie, `302` to `/`.
  - The login cookie is set with `httponly=True, samesite="Lax", secure=True, max_age=SESSION_MAX_AGE_SECONDS`.
  - `_valid_sessions` is deleted. `_pending_logins` stays in memory (a login in flight across a restart is already handled by "start over").

- [ ] **Step 1: Write the failing tests**
  - `test_a_session_survives_a_process_restart`: create a session, then `db.use_path_for_tests(same_path)` to force a reconnect; `session_for` of a request carrying the token returns `{"username": "aryan", "role": "admin"}`.
  - `test_an_expired_session_is_rejected_and_removed`: `create_session(..., now=0)`; `session_for(req, now=SESSION_MAX_AGE_SECONDS + 1)` is `None`, and the row count is 0.
  - `test_logout_deletes_the_session_and_clears_the_cookie`: after `handle_logout`, `session_for` is `None`, the response is a 302 to `/`, and `Set-Cookie` for `dashboard_session` has `Max-Age=0` or an `expires` in the past.
  - `test_the_login_cookie_is_secure_httponly_and_lax`: drive `handle_callback` with `aioresponses` mocking GitHub's token and user endpoints for an authorized user; the `Set-Cookie` header contains `Secure`, `HttpOnly`, `SameSite=Lax`, `Max-Age=2592000`.
  - Update `test_a_valid_session_cookie_reaches_the_handler` to use `create_session("aryan", "admin")` instead of `_valid_sessions.add`.
- [ ] **Step 2:** Run `PYTEST tests/test_auth.py`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement per Interfaces. `handle_callback` decides the role with `role_for_username` from Task 5; until Task 5 lands, treat every authorized user as `"admin"`.
- [ ] **Step 4:** Run `PYTEST tests/test_auth.py`. Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 5: Roles and segment-matched open paths

**Files:**
- Modify: `mac mini/Stream/auth.py`
- Modify: `mac mini/Stream/config.example.json` (add `"editor_github_username": ""`)
- Test: `mac mini/Stream/tests/test_auth.py`

**Interfaces:**
- Consumes: `create_session`, `session_for` (Task 4).
- Produces:
  - `auth.role_for_username(username: str) -> str | None`: `"admin"` if it is in `authorized_github_usernames` (case-insensitive), `"editor"` if it equals `editor_github_username` (case-insensitive, and non-empty), else `None`. Admin wins if both match.
  - `auth.path_matches(path: str, prefix: str) -> bool`: `path == prefix or path.startswith(prefix + "/")`.
  - `auth.OPEN_PATHS`: the current tuple moved to module level, unchanged in content. `"/"` matches exactly; every other entry goes through `path_matches`.
  - `auth.EDITOR_PATHS = ("/editor", "/api/editor", "/auth/logout")`
  - Middleware order: open path → handler. No session → `/admin` or `/editor` (via `path_matches`) redirects to `/auth/login?next=...`; `/api/` gets 401 JSON (unchanged text); anything else gets the 401 text. Admin session → handler. Editor session on an `EDITOR_PATHS` path → handler. Editor session elsewhere → `/api/` paths get `403` JSON `{"error": "This account can only use the editor pages."}`; other paths get `302` to `/editor`.
  - `handle_callback`: an account with role `None` keeps today's 403. An editor whose `next_path` is not under `EDITOR_PATHS` is sent to `/editor`.

- [ ] **Step 1: Write the failing tests**
  - `test_role_for_username`: admin list `["DualBladeX"]`, editor `"CutGuy"` → `role_for_username("dualbladex") == "admin"`, `("cutguy") == "editor"`, `("someone") is None`; with `editor_github_username` set to `""`, `role_for_username("") is None`.
  - `test_open_paths_match_whole_segments`: `/clipsadmin`, `/healthz`, `/api/publicity`, `/_nextfoo` → 401; `/clips/a.mp4`, `/api/public/clips`, `/_next/static/x.js`, `/health` → handler ran.
  - `test_an_editor_session_cannot_reach_admin_apis`: for each of `/api/config`, `/api/config/schema`, `/api/points/x`, `/api/logs`, `/api/status`, an editor-session request → 403 with JSON body `{"error": "This account can only use the editor pages."}`.
  - `test_an_editor_visiting_admin_is_sent_to_the_editor_pages`: editor session on `/admin` → `HTTPFound` to `/editor`.
  - `test_an_editor_session_reaches_editor_paths`: `/editor`, `/api/editor/projects`, `/auth/logout` → handler ran.
  - `test_an_admin_session_reaches_everything`: `/api/config` and `/api/editor/projects` → handler ran.
- [ ] **Step 2:** Run `PYTEST tests/test_auth.py`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement per Interfaces. Keep the `OPEN_PATHS` comments, since they record why each agent route is listed.
- [ ] **Step 4:** Run `PYTEST`. Expected: passes (the existing `/api/ocr/*` and `/api/game/state` tests must still pass).
- [ ] **Step 5: Checkpoint.**

---

### Task 6: Masked config API, setting metadata, server-side validation

**Files:**
- Create: `mac mini/Stream/config_schema.py`
- Modify: `mac mini/Stream/dashboard_api.py` (`get_config`, `update_config`, new `get_config_schema`, route registration)
- Test: `mac mini/Stream/tests/test_config_schema.py`, `mac mini/Stream/tests/test_dashboard_config.py`

**Interfaces:**
- Produces in `config_schema.py`:
  - `SECRET_MASK = "••••••••"`
  - `FIELD_TYPES = ("number", "text", "bool", "list", "table", "json", "secret")`
  - `SECTIONS = ["Streamer.bot", "Points (Cloudbot)", "Roulette", "Credit OCR", "Spotify", "Rank (!rank)", "Greeter", "Discord go-live", "Twitch & YouTube", "Break screen", "Login & site", "Editor app", "Other"]`
  - `FIELDS: dict[str, dict]`: one entry for **every non-underscore key in `config.example.json`** (85 today, plus `app_db_path` and `editor_github_username` added by this plan). Each entry is `{"label": str, "description": str, "section": str, "type": str}` plus optional `"unit": str`, `"min": number`, `"max": number`. Labels are plain English ("Voting window"); descriptions are one line. Every key containing `secret`, `token`, `password`, `api_key` or `webhook`, and `henrik_api_key` and `youtube_api_key`, has type `"secret"`. The `min`/`max` values come from what the owning module already clamps (e.g. `discord_live_poll_seconds` min 15, from `discord_live._poll_seconds`); add none that the code does not already imply.
  - `is_secret(key: str, data: dict) -> bool`: true for `FIELDS` entries of type `"secret"`, for `_custom_fields` entries of type `"secret"`, and, for keys known to neither, when the key matches `secret|token|password|api_key|webhook` (case-insensitive).
  - `masked(data: dict) -> dict`: a copy with every secret replaced by `SECRET_MASK` if truthy, else `""`. A secret that is unset must still read as falsy, because `ConfigEditor`'s Streamlabs/Spotify connect rows test `Boolean(config.x_token)`.
  - `schema_for(data: dict) -> dict`: `{"sections": SECTIONS, "fields": {...}}` covering every non-underscore key in `data`. Order of precedence: `FIELDS`, then `_custom_fields[key]` (label = key, description `""`), then inferred, with section `"Other"`. Inference: bool → `"bool"`; int/float → `"number"`; list → `"list"`; dict whose values are all str/int/float/bool → `"table"`; other dict → `"json"`; str → `"secret"` if `is_secret`, else `"text"`.
  - `validate_update(updates: dict, data: dict) -> tuple[str, str] | None`: returns `(key, message)` for the first invalid entry, else `None`. Rules:
    - A new key must match `^[a-z][a-z0-9_]*$`, message `"Key must be lowercase letters, digits and underscores"`.
    - Underscore keys other than `_custom_fields` are refused.
    - Per type: `number` is int/float, not bool, within min/max, message `"Must be {min} {unit} or more"` / `"Must be {max} {unit} or less"` (unit omitted when absent); `bool` is bool; `list` is a list of str; `table` is a dict of scalars; `json` is any JSON value; `text`/`secret` are str.
    - `_custom_fields` must be a dict of `{"type": one of FIELD_TYPES, "section": one of SECTIONS}`.
- Produces in `dashboard_api.py`:
  - `GET /api/config` → `masked(config.all())`.
  - `GET /api/config/schema` → `schema_for(config.all())`, with header `Cache-Control: no-store`.
  - `PUT /api/config`: body is a flat dict of changed keys. Drop entries whose value equals `SECRET_MASK`. Run `validate_update`; on error return `400 {"error": message, "field": key}` and write **nothing**. Otherwise set each key, merge `_custom_fields` into the existing dict (not replace it), `config.save()`, and return `{"status": "ok", "updated_keys": [...]}`.

- [ ] **Step 1: Write the failing tests**
  - `test_every_example_key_has_metadata`: for each non-underscore key of `config.example.json`, it is in `FIELDS` with a non-empty label, a non-empty description, a section in `SECTIONS`, and a type in `FIELD_TYPES`.
  - `test_secrets_are_masked_and_unset_secrets_stay_empty`: `masked({"github_client_secret": "abc", "spotify_refresh_token": "", "http_port": 8765})` equals `{"github_client_secret": SECRET_MASK, "spotify_refresh_token": "", "http_port": 8765}`.
  - `test_an_unknown_key_that_looks_secret_is_masked`: `masked({"new_service_api_key": "zzz"})["new_service_api_key"] == SECRET_MASK`.
  - `test_schema_infers_unknown_keys_into_other`: `schema_for({"foo_list": ["a"], "foo_map": {"a": [1]}})["fields"]` gives `"list"` and `"json"` in section `"Other"`.
  - `test_validate_number_bounds`: a key with `"min": 15, "unit": "seconds"` given `5` → `(key, "Must be 15 seconds or more")`.
  - `test_validate_rejects_bad_new_key_and_private_keys`: `"Bad-Key"` and `"_x"` are refused.
  - `test_get_config_never_returns_a_raw_secret` (aiohttp test client against an app with `register_routes`, and `config._data` monkeypatched to hold `"github_client_secret": "realvalue"`): `"realvalue"` does not appear in the response text.
  - `test_put_with_the_mask_keeps_the_stored_secret`: PUT `{"github_client_secret": SECRET_MASK, "http_port": 9000}` → stored secret still `"realvalue"`, port `9000`. Monkeypatch `config.save` to a no-op recorder.
  - `test_put_with_one_invalid_value_writes_nothing`: PUT `{"http_port": 9000, "discord_live_poll_seconds": 1}` → 400 with `field == "discord_live_poll_seconds"`, `http_port` unchanged, `save` not called.
  - `test_put_merges_custom_fields`: existing `_custom_fields = {"a_key": {...}}`, PUT adds `b_key` with `_custom_fields: {"b_key": {"type": "number", "section": "Editor app"}}` → both entries present.
- [ ] **Step 2:** Run `PYTEST tests/test_config_schema.py tests/test_dashboard_config.py`. Expected: FAIL (`No module named 'config_schema'`).
- [ ] **Step 3:** Implement `config_schema.py` and the three handlers per Interfaces. Write each `FIELDS` description from what the owning module does with the key, not from the key's name.
- [ ] **Step 4:** Run `PYTEST`. Expected: passes.
- [ ] **Step 5: Checkpoint.**

---

### Task 7: Frontend dependency upgrades and a working linter

**Files:**
- Modify: `mac mini/Site/package.json`, `mac mini/Site/package-lock.json`
- Create: `mac mini/Site/eslint.config.mjs`

**Interfaces:** none.

- [ ] **Step 1:** `npm install next@16.4.3 eslint-config-next@16.4.3`, then `npm audit fix` (no `--force`).
- [ ] **Step 2:** Run `npm audit --omit=dev`. Expected: `found 0 vulnerabilities`, or only advisories whose fix needs `--force`; list any of those in the task report rather than forcing.
- [ ] **Step 3:** Create `eslint.config.mjs` as the flat config `eslint-config-next` documents for its installed version (core-web-vitals plus TypeScript), ignoring `out/`, `.next/`, `node_modules/`, and `public/dragon-original.js` (vendored CodePen code).
- [ ] **Step 4:** Run `npm run lint`. Expected: it runs to completion. Fix real errors in source. Pre-existing warnings may stay; list their count in the report.
- [ ] **Step 5:** Run `npx vitest run` and `npx next build`. Expected: all tests pass, and the build produces `/`, `/_not-found`, `/admin`.
- [ ] **Step 6: Checkpoint.**

---

### Task 8: Settings model (pure logic)

**Files:**
- Create: `mac mini/Site/lib/settingsModel.ts`
- Test: `mac mini/Site/lib/settingsModel.test.ts`

**Interfaces:**
- Produces:
  - `export const SECRET_MASK = "••••••••";`
  - `export type FieldType = "number" | "text" | "bool" | "list" | "table" | "json" | "secret";`
  - `export type FieldMeta = { label: string; description: string; section: string; type: FieldType; unit?: string; min?: number; max?: number };`
  - `export type Schema = { sections: string[]; fields: Record<string, FieldMeta> };`
  - `export type NewField = { key: string; type: FieldType; section: string; value: unknown };`
  - `groupBySection(schema: Schema): { section: string; keys: string[] }[]`: in `schema.sections` order, keys sorted by label, empty sections dropped.
  - `searchFields(schema: Schema, query: string): string[]`: case-insensitive match on key, label and description; an empty query returns `[]`.
  - `validateField(meta: FieldMeta, value: unknown): string | null`: the same rules and exact messages as backend `validate_update` for one value. For `json` fields the editor holds the raw text, so a `string` value that does not `JSON.parse` returns `"Not valid JSON"`.
  - `validateNewKey(key: string, existing: Record<string, unknown>): string | null`: `"Key must be lowercase letters, digits and underscores"` / `"That key already exists"`.
  - `changedKeys(original: Record<string, unknown>, edited: Record<string, unknown>): string[]`: keys whose JSON serialization differs, plus keys new in `edited`.
  - `buildPayload(original, edited, newFields: NewField[]): Record<string, unknown>`: only changed keys. A secret still equal to `SECRET_MASK` is never included. `json` text is parsed into its value. When `newFields` is non-empty, it adds their values plus `_custom_fields` = the original `_custom_fields` merged with `{[key]: {type, section}}`.

- [ ] **Step 1: Write the failing tests**
  - `groupBySection drops empty sections and keeps section order`
  - `searchFields matches description text` (query `"chat"` finds a field whose description mentions chat but whose key does not)
  - `validateField enforces min with unit` → `"Must be 5 seconds or more"`
  - `validateField flags invalid JSON text for json fields` → `"Not valid JSON"`
  - `buildPayload omits untouched secrets` (original and edited both hold `SECRET_MASK` for `github_client_secret` → key absent)
  - `buildPayload sends a replaced secret` (edited holds `"newvalue"` → present)
  - `buildPayload sends only changed keys`
  - `buildPayload merges custom field metadata` (original `_custom_fields: {a: {...}}` plus a new field `b` → payload `_custom_fields` has both `a` and `b`)
- [ ] **Step 2:** Run `npx vitest run lib/settingsModel.test.ts`. Expected: FAIL (module not found).
- [ ] **Step 3:** Implement per Interfaces.
- [ ] **Step 4:** Run `npx vitest run lib/settingsModel.test.ts`. Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 9: `useConfigEditor` with schema, diff saves and old-backend fallback

**Files:**
- Modify: `mac mini/Site/lib/useConfigEditor.ts` (replace the docstring, which still says the endpoint "deliberately shows everything")
- Test: `mac mini/Site/lib/useConfigEditor.test.ts`

**Interfaces:**
- Consumes: `Schema` (Task 8).
- Produces: `useConfigEditor(): { config: Record<string, unknown> | null; schema: Schema | null; schemaMissing: boolean; loading: boolean; error: boolean; saving: boolean; saveError: string | null; saveErrorField: string | null; saveSuccess: boolean; save: (payload: Record<string, unknown>) => Promise<void> }`
  - Fetches `/api/config` and `/api/config/schema` in parallel. A schema 404 sets `schemaMissing: true` and leaves `error` false. Any other failure sets `error`.
  - `save(payload)` PUTs the payload as-is. On 400 it sets `saveError` from `body.error` and `saveErrorField` from `body.field`. On success it re-fetches both endpoints (so masks and the inferred schema reflect the saved state) and sets `saveSuccess`.

- [ ] **Step 1: Write the failing tests**
  - `loads config and schema together`
  - `a 404 schema sets schemaMissing without an error`
  - `save reports the backend's field-level error` (400 `{error: "Must be 15 seconds or more", field: "discord_live_poll_seconds"}`)
  - `save re-fetches after success` (fetch called 4 times: two loads, PUT, two reloads → assert the GET count is 4 and the PUT count is 1)
- [ ] **Step 2:** Run `npx vitest run lib/useConfigEditor.test.ts`. Expected: the new tests FAIL.
- [ ] **Step 3:** Implement per Interfaces, keeping `credentials: "same-origin"` on every call.
- [ ] **Step 4:** Run `npx vitest run lib/useConfigEditor.test.ts`. Expected: PASS.
- [ ] **Step 5: Checkpoint.**

---

### Task 10: Settings page UI

**Files:**
- Modify: `mac mini/Site/components/admin/ConfigEditor.tsx` (rewrite; the `ConnectRow` component and its two uses stay)
- Modify: `mac mini/Site/components/admin/AdminDashboard.tsx:25` (tab hint `"Raw JSON, accounts"` → `"Settings, accounts"`)
- Modify: `mac mini/Site/app/admin/page.tsx` (drop the stale "Config and Points are still placeholders" comment)
- Test: `mac mini/Site/components/admin/ConfigEditor.test.tsx` (rewrite)

**Interfaces:**
- Consumes: `useConfigEditor` (Task 9), and `groupBySection`, `searchFields`, `validateField`, `validateNewKey`, `buildPayload`, `changedKeys`, `SECRET_MASK` (Task 8).
- Behavior, matching the "Settings editor" wireframe board (https://claude.ai/artifact/XG2WnQFHr4QEr685HawViy). Use the dashboard's existing classes and tokens (`border-seam`, `bg-bezel`, `bg-ink`, `text-phosphor`, `text-dim`, `text-rec`, `text-field`, `wallpaper`), not the wireframe's colors.
  - A section sidebar (`<nav aria-label="Settings sections">`) of buttons with each section's field count, plus a final **Advanced (raw JSON)** entry.
  - A search input labelled "Search all settings". When non-empty it shows matching fields from every section, grouped by section.
  - One row per field: label, description, the key in a `<code>`, and an input by type: `number` → `<input type="number">` plus unit text; `text` → text input; `bool` → checkbox; `list` → removable chips plus an **Add** text input; `table` → a key/value grid, values as number or text inputs by their current type, with add and remove row; `json` → a monospace textarea of `JSON.stringify(value, null, 2)`; `secret` → the mask (or "Not set"), a Set/Not set badge and a **Replace…** button that reveals a password input (leaving it empty and pressing Cancel restores the mask).
  - The inline error under a field is `validateField`'s message; changed fields get a visible changed style; a backend `saveErrorField` shows its message on that field.
  - A sticky bar appears only while there are changes: `"{n} unsaved change(s)"`, plus `" · {m} error(s) to fix before saving"` when there are errors. **Discard** resets to the loaded config. **Save** is disabled while errors exist and sends `buildPayload(...)`.
  - **+ Add setting** opens a form with key, type (select of `FieldType`), value (input by type), section (select of `schema.sections`, default `"Other"`) and **Add**, which runs `validateNewKey` and stages the field in the unsaved changes.
  - **Advanced (raw JSON)**: the masked config in a textarea. Save parses it (keeping the existing message "That's not valid JSON - check for a stray comma or missing quote.") and sends only `changedKeys`, never mask-valued secrets.
  - With `schemaMissing`, open on Advanced and show "This backend is older than the site - showing raw JSON until it is updated."

- [ ] **Step 1: Write the failing tests** (mock `fetch` with `asFetchMock` from `lib/testUtils`):
  - `renders sections with counts and the fields of the selected section`
  - `search finds a field in another section by its description`
  - `shows the mask for a set secret and never renders its value` (the config fixture's secret is the mask; assert no element contains any other value for it)
  - `blocks save while a field is invalid` (number below min → message shown, Save disabled)
  - `saves only the changed keys` (edit `http_port`; the PUT body equals `{"http_port": 9000}`)
  - `replacing a secret sends the new value`
  - `adds a new setting with its metadata` (PUT body includes the key and `_custom_fields`)
  - `a nested value renders as JSON and invalid JSON blocks save` (`streamerbot_subscribe_events`)
  - `falls back to raw JSON when the schema endpoint is missing` (the schema fetch returns 404 → the notice text and a textarea are shown)
  - Keep the existing invalid-raw-JSON test, now driven through the Advanced view.
- [ ] **Step 2:** Run `npx vitest run components/admin/ConfigEditor.test.tsx`. Expected: FAIL.
- [ ] **Step 3:** Implement per Behavior. Split helpers into the same file unless it passes ~400 lines; past that, move field renderers into `components/admin/SettingsField.tsx`.
- [ ] **Step 4:** Run `npx vitest run` and `npx next build`. Expected: all pass, and the build succeeds.
- [ ] **Step 5: Checkpoint.**

---

### Task 11: Documentation and shipping

**Files:**
- Modify: `CLAUDE.md` (backend tests now run here via the uv command; `auth.py` roles and segment matching; sessions in SQLite `app.db`; `/api/config` is masked and `config_schema.py` is the metadata source, and new config keys need a `FIELDS` entry or `test_every_example_key_has_metadata` fails; drop the stale `npm run lint` and `requirements.txt` known-gap lines)
- Modify: `mac mini/Stream/README.md` only where it contradicts the above

- [ ] **Step 1:** Run `PYTEST`, `npx vitest run` and `npx next build` one final time. Expected: as in Tasks 6 and 10.
- [ ] **Step 2:** Update `CLAUDE.md` per Files.
- [ ] **Step 3: Ship (needs the user's go-ahead before pushing).** Clone `aryanverma7/stream-backend` and `aryanverma7/stream-site` into a scratch directory, copy the changed files in, add `app.db`, `app.db-wal` and `app.db-shm` to the backend's `.gitignore`, commit each repo, and push.
- [ ] **Step 4: Mac Mini steps for the user** (they cannot be done from here). Hand over as a checklist:
  1. `git pull` both repos.
  2. `venv/bin/pip install -r requirements.txt`.
  3. Run the test suite.
  4. `npm install && npm run build` in the site.
  5. Restart via `launchctl unload/load ~/Library/LaunchAgents/com.dualbladex.backend.plist`.
  6. Confirm `node --version` ≥ 24.22.1 (or 22.23.4).
  7. Open `~/Library/LaunchAgents/com.dualbladex.backend.plist` and confirm `ProgramArguments[0]` is the venv's `python3`, not `/usr/bin/python3` or a `/path/to/` placeholder.
  8. Log in again (all old in-memory sessions are gone).
