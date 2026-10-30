# Editor App — Plan 3 of 5: Discord DMs and notifications

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tell each person what needs them, without spamming either. That means:
- one DM to the editor when a VOD or clip is ready
- hourly reminders (12:00–22:00) when he has downloaded a VOD but not confirmed its delete
- a DM to the streamer when a new version lands
- batched comment/reply digests after ~10 minutes of quiet
- a "site is back online" DM after an outage
- an in-site notification feed (the bell)

Plan 2's two carried fixes come first.

**Architecture:** Plan 2's `events` table is the single source. One scheduler job (`notifier.run`) walks new events from a stored cursor. It turns them into rows in a durable **outbox** table, each with a unique `dedupe_key`, so a crash or restart can never send twice and never loses a message. A second job delivers the outbox through Discord's REST API (create a DM channel, post a message; no gateway connection) with backoff. Reminders and digests are absolute times in SQLite, like everything else.

**Tech Stack:** Python 3.14, aiohttp 3.14.5 (HTTP client for Discord), stdlib sqlite3, pytest + pytest-asyncio + aioresponses.

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md` ("Threads and notifications", Flow 1 "VOD ready", Flow 2 reminders, "Power cuts" missed reminders and back-online). **Roadmap:** `docs/superpowers/plans/2026-10-25-editor-app-roadmap.md` (Plan 3 rows, "Carried from Plan 2's final review", and "What each plan hands on → Plan 2").

## Global Constraints

- Repo: `mac mini/Stream`, branch `editor-app-plan-3` (created from `main` at a04c713). Stage files by name. Never push.
- **PYTEST**, from `mac mini/Stream`: `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings`. Baseline: 765 passed, 12 failed (all in `test_credit_ocr.py`; pre-existing fixture problems plus no tesseract here).
- Plan 2's rules hold:
  - `db.transaction()` for multi-statement writes, never `with conn:`
  - never hold a transaction across an `await`
  - `now` is injected
  - loggers via `from logger import get_logger`
  - new config keys get a `config_schema.FIELDS` entry (section "Editor app") and a `config.example.json` line
  - async tests carry `@pytest.mark.asyncio`
- Discord constants (exact):
  - `API = "https://discord.com/api/v10"`
  - auth header `Authorization: Bot <token>`
  - create DM: `POST {API}/users/@me/channels` with JSON `{"recipient_id": "<id>"}` → `{"id": "<channel id>"}`
  - send: `POST {API}/channels/{channel_id}/messages` with JSON `{"content": "<text>"}`
  - 429: wait `retry_after` seconds (JSON body, float) and retry once in the same call, otherwise back off
  - message content ≤ 2000 characters (truncate with `…`)
- Values (exact):
  - digest quiet time `DIGEST_QUIET_SECONDS = 600`
  - reminders start `REMINDER_DELAY_SECONDS = 3 * 3600` after the editor's download click
  - reminders repeat every `REMINDER_EVERY_SECONDS = 3600`
  - only while local time satisfies `REMINDER_START_HOUR <= tm_hour < REMINDER_END_HOUR` (12 and 22), so the last reminder of a day goes out before 22:00
  - back-online threshold `OUTAGE_SECONDS = 600`
  - outbox backoff uses `scheduler.backoff_seconds(attempts)`
- Recipients are chosen by role:
  - **the editor** is `config.editor_discord_user_id`
  - **the streamer** is the new `config.streamer_discord_user_id`
  - an empty id means that person gets no DMs (logged once at info level, never an error)
- `discord_notifications_enabled` (bool, default false) switches all DMs off. The in-site bell still works.
- The streamer's username is any of `authorized_github_usernames` (case-insensitive). The editor's is `editor_github_username`. An event's "other person" is the role opposite to the actor's; `actor == "system"` notifies nobody unless a rule below says otherwise.

## Review Focus

1. **A crash between "queued" and "sent", or between "sent" and "marked sent".** The outbox must never send the same `dedupe_key` twice from a cold start, except in the one unavoidable case where Discord accepted the message and the process died before recording it. Document that case as accepted. Pinned in Task 3 (`test_outbox_dedupes_and_survives_restart`).
2. **The Mac Mini is down for 6 hours across reminder time.** The editor gets one catch-up reminder, not six. Pinned in Task 5 (`test_outage_sends_one_catch_up_not_a_burst`).
3. **Twenty comments in a row.** That gives one digest DM ten minutes after the last one, not twenty DMs and not one per minute. Pinned in Task 4 (`test_burst_of_comments_gives_one_digest_after_quiet`).
4. **The Discord token is wrong or revoked, or the bot isn't in a server with the user.** The outbox rows stay pending with backoff, the storage view shows the last error, and nothing else breaks. Pinned in Tasks 3 and 6.
5. **The bell for the editor must never show the streamer's own personal data.** It shows events only (no to-dos), and only events by the *other* person. Pinned in Task 6 (`test_bell_lists_only_other_persons_events`).

---

### Task 1: Carried fixes from Plan 2

**Files:** Modify `editor_service.py`, `deletions.py`. Tests: `tests/test_editor_service.py`, `tests/test_deletions.py`.

**Interfaces:**
- `complete_iteration_upload` raises `ValueError("This tape's files are being deleted")` while a `tape_files` request for the iteration's tape is pending or running. Add `deletions.ensure_tape_not_deleting(tape_id)`, next to `ensure_project_not_deleting`.
- `deletions.run_due`'s forward-jump check compares the wall-clock delta with the `time.monotonic()` delta since the previous call. It counts as a jump only when `(now - _prev_now) - (mono - _prev_mono) > CLOCK_JUMP_SECONDS`. Keep `_prev_mono` beside `_prev_now`, and make `_reset_for_tests` clear both. Inject the monotonic clock as `run_due(now, mono=None)` for tests.

- [ ] **Step 1: Write the failing tests:**
  - `test_complete_refused_while_tape_files_delete_is_live`
  - `test_slow_tick_is_not_a_clock_jump`: wall clock +900 s and monotonic +900 s → no re-grace
  - `test_wall_jump_without_monotonic_is_a_jump`: wall clock +900 s, monotonic +30 s → grace
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST. Expected: no new failures.
- [ ] **Step 5: Commit:** "editor: carried fixes from Plan 2 review".

---

### Task 2: Discord REST client and config keys

**Files:** Create `discord_dm.py`. Modify `config.example.json`, `config_schema.py`. Test: `tests/test_discord_dm.py`.

**Interfaces:**
- Config keys, all in "Editor app":
  - `discord_bot_token` (secret)
  - `streamer_discord_user_id` (text)
  - `discord_notifications_enabled` (bool, default false)
- `class DiscordNotConfigured(Exception)`: the token is empty
- `class DiscordError(Exception)` with `.status: int` (0 for network/timeout) and `.retry_after: float | None`
- `async discord_dm.send_dm(user_id: str, content: str) -> str`:
  1. Gets or creates the DM channel. The channel id is cached in `db.meta` under `discord_dm_channel_<user_id>`; if a send to a cached channel returns 404, the cache is dropped and the channel created again, once.
  2. Posts the message (truncated to 2000 characters).
  3. Returns the message id.
  4. On a 429 it sleeps `retry_after` (at most 10 s) and retries once; otherwise it raises `DiscordError` with `retry_after`.
  5. A 401/403 raises `DiscordError` with that status.
- No token, user id or message content is ever logged. The status and Discord's `code`/`message` fields may be logged.
- A 15 s total timeout per request.

- [ ] **Step 1: Write the failing tests** (aioresponses):
  - `test_send_creates_channel_once_and_caches_it`
  - `test_cached_channel_404_recreates_once`
  - `test_429_waits_and_retries_once` (monkeypatch `asyncio.sleep`)
  - `test_403_raises_with_status`
  - `test_empty_token_raises_not_configured`
  - `test_long_content_truncated_to_2000`
  - `test_network_error_is_status_0`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST, including `test_every_example_key_has_metadata`.
- [ ] **Step 5: Commit:** "discord: REST DM client and config keys".

---

### Task 3: Durable outbox

**Files:** Create `notify_outbox.py`. Test: `tests/test_notify_outbox.py`.

**Interfaces:**
- Table:
  ```
  dm_outbox(
    id INTEGER PRIMARY KEY,
    recipient_role TEXT NOT NULL CHECK (recipient_role IN ('streamer','editor')),
    content TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending','sending','sent','dropped')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
  )
  ```
- `enqueue(recipient_role, content, dedupe_key, now=None) -> bool`: INSERT OR IGNORE. Returns False when the key exists, so it is idempotent.
- `async deliver_due(now) -> None`, a scheduler job:
  1. When `discord_notifications_enabled` is false, return without touching anything.
  2. For each `pending` row with `next_attempt_at <= now` (oldest first, at most 20 per tick): resolve the recipient's user id. If it is empty, mark the row `dropped` with `last_error="no Discord id for <role>"`.
  3. Otherwise mark it `sending` (commit), then await `send_dm` with no transaction open.
     - On success, mark it `sent` with `sent_at`.
     - On `DiscordError` or `DiscordNotConfigured`, set it back to `pending` with `attempts+1`, `next_attempt_at = now + max(backoff_seconds(attempts), retry_after or 0)` and `last_error`.
  It never raises.
- `reset_stuck_sending() -> int`: `sending` → `pending` (called once at startup). A row stuck in `sending` may already have been delivered; that rare duplicate is accepted, so say so in a comment.
- `status() -> dict`: `{"enabled", "pending", "dropped", "last_error", "last_sent_at"}`

- [ ] **Step 1: Write the failing tests:**
  - `test_enqueue_is_idempotent_by_key`
  - `test_outbox_dedupes_and_survives_restart`: enqueue, `use_path_for_tests(same)`, enqueue the same key again → one row
  - `test_disabled_sends_nothing`
  - `test_failure_backs_off_and_keeps_pending`
  - `test_missing_recipient_id_drops_with_reason`
  - `test_stuck_sending_reset_at_startup`
  - `test_retry_after_is_respected`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST.
- [ ] **Step 5: Commit:** "notify: durable DM outbox".

---

### Task 4: Notifier: events to immediate DMs and batched digests

**Files:** Create `notifier.py`. Test: `tests/test_notifier.py`.

**Interfaces:**
- Consumes:
  - `editor_store.events_since(after_id, exclude_actor=None)` and the event fields (`id, at, actor, kind, project_id, tape_id, iteration_id, comment_id, summary`)
  - `editor_store.get_project`, `get_tape`, `get_iteration`
  - `notify_outbox.enqueue`
  - `auth.role_for_username`
- Tables:
  - `notify_cursor` in `db.meta` (key `notify_cursor`, last processed event id)
  - `digest_pending(recipient_role TEXT NOT NULL, event_id INTEGER NOT NULL UNIQUE, actor TEXT NOT NULL, at REAL NOT NULL)`
- `async run(now) -> None`, a scheduler job:
  1. Read the events after the cursor, in id order. For each event, inside one transaction per event, do one of the following.
     - **Immediate rules:**
       - `vod_ready` (any actor, including `system`/`agent`) goes to the **editor**: `"New {kind} ready: {project name}. Open the editor page to download it."`, where kind is "VOD" or "clip". The dedupe key is `vod_ready:<project_id>`.
       - `iteration_ready` by the editor (or `system`) goes to the **streamer**: `"{tape name} v{version} is ready to review."`. The key is `iteration_ready:<iteration_id>`.
     - **Digest kinds:** `comment_added`, `reply_added`, `comment_resolved`, `start_over`. Only for an actor whose role is admin/editor: insert into `digest_pending` for the other role.
     - Every other kind is ignored.
  2. Advance the cursor in that same transaction.
  3. **Flush:** for each recipient with pending digest rows whose newest `at` is at least `DIGEST_QUIET_SECONDS` old, build one message and delete those rows in one transaction with the enqueue. The message is grouped per tape and version, e.g. `"Aryan: 6 comments, 2 replies on Jett dash v2; 1 comment on Clip 0412."`. Use the actor's GitHub username as written in the event, and `"resolved"`/`"started over"` wording for those kinds. The key is `digest:<recipient>:<max event id>`.
  It never raises.
- `display_name(username) -> str` returns the username as stored. Keep it as a function so a nicer name can be added later.

- [ ] **Step 1: Write the failing tests:**
  - `test_vod_ready_dms_editor_once`, also covering the clip wording
  - `test_iteration_ready_dms_streamer`
  - `test_own_events_never_notify_yourself` (the streamer comments → the editor's digest only)
  - `test_burst_of_comments_gives_one_digest_after_quiet`: 20 comments at t…t+190 s; `run` at t+700 sends nothing; at t+791 one digest that names 20 comments
  - `test_cursor_survives_restart_no_double_processing`
  - `test_digest_groups_by_tape_and_version`
  - `test_system_actor_digests_nothing`
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST.
- [ ] **Step 5: Commit:** "notify: immediate DMs and quiet-time digests from events".

---

### Task 5: Delete-not-confirmed reminders

**Files:** Create `reminders.py`. Test: `tests/test_reminders.py`.

**Interfaces:**
- State: `db.meta` key `reminder_next_<project_id>` (an absolute time).
- `async run(now) -> None`, a scheduler job, for every project with `download_clicked_at` set, `vod_status` in (`ready`) and no pending/running `vod` delete request:
  1. `due_from = download_clicked_at + REMINDER_DELAY_SECONDS`.
  2. `next = float(meta) if set else due_from`.
  3. If `now >= next` and the local time is inside the window: enqueue to the **editor** `"Reminder: you downloaded {name} — delete it from the editor page so it stops using Drive space."` with key `reminder:<project_id>:<int(now // 3600)>`, then set meta to `now + REMINDER_EVERY_SECONDS`.
  4. If `now >= next` but the time is outside the window: do nothing, so the next tick inside the window sends exactly one.
  5. When the project leaves the condition (delete requested or done), delete its meta key.
  It never raises.

- [ ] **Step 1: Write the failing tests** (inject `now`; compute local hours with `time.localtime`; tests set `TZ` via monkeypatch + `time.tzset()`):
  - `test_no_reminder_before_three_hours`
  - `test_hourly_inside_window`
  - `test_quiet_outside_window_then_one_at_noon`
  - `test_outage_sends_one_catch_up_not_a_burst`: the clock jumps 6 h → one enqueue
  - `test_stops_once_delete_requested`
  - `test_admin_click_does_not_start_reminders` (reminders key off `download_clicked_at`, which only the editor's click sets; assert via the API flow or a direct store setup with a comment)
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST.
- [ ] **Step 5: Commit:** "notify: delete-not-confirmed reminders, 12:00-22:00".

---

### Task 6: Back-online DM, bell API, status, wiring

**Files:**
- Create `editor_bell.py`.
- Modify `editor_api.py`, `main.py`, `scheduler.py` (only to expose the previous `clock_last_seen` at boot, if needed).
- Tests: `tests/test_editor_bell.py`, `tests/test_editor_api.py`, `tests/test_notifier.py`.

**Interfaces:**
- **Back online:**
  - At startup, before the scheduler starts, read `prev = scheduler._last_seen()` (or a public `scheduler.last_seen()`; add it).
  - On the **first sane tick**, a job `notifier.back_online_once(now)` checks: if `prev` is set and `now - prev > OUTAGE_SECONDS`, enqueue to the **editor** `"The editor site is back online (it was down for about {minutes} min)."` with key `back_online:<int(prev)>`.
  - It runs once per process.
- **Bell:**
  - `editor_bell.feed(username, after_id=None, limit=50) -> dict` returns `{"items": [...], "unseen": int}`.
  - Items are events by anyone **other than** `username`, excluding `system`, newest first, each `{id, at, actor, kind, summary, project_id, tape_id, iteration_id}`.
  - `unseen` counts the items with id greater than `meta["bell_seen_<username>"]`.
  - `mark_seen(username, up_to_id)` stores the max.
- **Routes** (both roles, identity from the session):
  - `GET /api/editor/notifications?after=` → `feed`
  - `POST /api/editor/notifications/seen` `{up_to_id}` → 200
- **Status:** `GET /api/editor/storage` also returns `"discord": notify_outbox.status()`.
- **Wiring in `main.py`:**
  1. `notify_outbox.reset_stuck_sending()` at startup
  2. register jobs in this order: `notifier.run`, `reminders.run`, `notify_outbox.deliver_due`, `notifier.back_online_once`
  3. all before `scheduler.start()`; keep the existing order of the other jobs

- [ ] **Step 1: Write the failing tests:**
  - `test_bell_lists_only_other_persons_events`
  - `test_bell_unseen_and_mark_seen`
  - `test_back_online_dm_after_long_gap_only_once`
  - `test_no_back_online_for_short_restart`
  - `test_storage_includes_discord_status` (API, admin)
  - `test_notifications_route_uses_session_identity` (the editor can't read the streamer's seen marker by passing a username)
- [ ] **Step 2:** Run them. Expected: FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Full PYTEST.
- [ ] **Step 5: Commit:** "notify: back-online DM, bell feed, status and startup wiring".

---

### Task 7: Setup guide, docs, roadmap

**Files:**
- Create `DISCORD_BOT_SETUP.md`.
- Modify `/home/aryan/projects/CLAUDE.md` and the roadmap. Both are outside the repo; do not commit them.

- [ ] **Step 1:** `DISCORD_BOT_SETUP.md` is for the streamer, as plain numbered steps:
  1. Create an application in the Discord Developer Portal, add a Bot, and copy its token into Settings → "Editor app" → Discord bot token.
  2. Invite the bot to your server. Use the OAuth2 URL generator with scope `bot`; it needs no permissions to send DMs, and it must share a server with both people.
  3. Turn on Discord Developer Mode, then right-click each person → Copy User ID, and paste the IDs into "Streamer Discord user ID" and "Editor Discord user ID".
  4. Both people must allow DMs from members of that server.
  5. Switch on "Discord notifications".
  6. What each DM means, and when reminders arrive (12:00–22:00 Mac Mini time).
  Describe console labels rather than quoting uncertain ones.
- [ ] **Step 2:** In CLAUDE.md, add a short subsection: events → notifier cursor → outbox (dedupe keys, the accepted duplicate case) → REST delivery, plus digests, reminders, back-online and the bell.
- [ ] **Step 3:** Roadmap:
  - tick the Plan 3 rows and the two "Carried from Plan 2" rows
  - set Plan 3's status to "built, not yet shipped"
  - add a "### Plan 3" hand-on: the bell routes, `notify_outbox.status()` in storage, the config keys, the event kinds that DM, and that Plan 5 must show the VOD-ready popup by reading the bell for `vod_ready`
- [ ] **Step 4:** Full PYTEST. Record the line.
- [ ] **Step 5: Commit** `DISCORD_BOT_SETUP.md`: "docs: Discord bot setup guide".
