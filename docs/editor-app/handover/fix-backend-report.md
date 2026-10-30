# Backend fix batch: report

Repo: /home/aryan/projects/mac mini/Stream, branch `audit-fixes`, not pushed.
I did all 22 items. Every fix has a test that fails without the fix: I stashed the source files, ran the tests (RED), then ran them again with the fix (GREEN).

## Commits (on top of fd76278)
- 3c9d444 fix(security): no sessions in backups, no Twitch secret in URLs, no control chars in next (items 1, 2, 3; also part of 21: Twitch token dropped on 401, Twitch ClientTimeout)
- 04a8c24 fix(points): refuse unsafe names, ignore forged Cloudbot replies (items 4, 5; also part of 21: _await_reply waiter, http_port integer)
- 3e5e37a fix(roulette): one session per double trigger, no stale timers, !agent per platform (items 7, 8, 22)
- 2496b0e fix(stream): tips switch on reconnect, robust widget broadcast, rotating log (items 6, 9, 10)
- f1200a2 fix(state): greeter and go-live state written atomically, greeter only on a newcomer (item 11)
- 356c392 fix(editor): tape-delete guard on uploads, best-effort VOD share, honest media links (items 12, 13, 14, 15)
- 3b44df3 fix(editor): scoped client_id, latest-ready start-over, deleted projects out of DMs and search (items 16, 17, 19, 20)
- bd01904 fix(uploader): project and its job_id meta row commit in one transaction (item 18)
- d23098e fix: small hardening from the audit (rest of item 21)

## Per item (RED -> GREEN)
1. backup.py: on the copy, `DELETE FROM sessions` + commit + VACUUM before upload. Test test_backup_copy_carries_no_session_tokens checks the uploaded bytes do not contain the token and the live DB keeps its session. RED 1 failed -> GREEN.
2. twitch_client: credentials go in the form body (`data=`). A new `TwitchError(what, status)` replaces raise_for_status, so the exception text has no URL. 15 s ClientTimeout. public_api live-status answers the fixed "Twitch live check failed" and logs only the exception type and status. discord_live logs the type and status only. uploader_api:147 already logged only the type name, so it needed no change. Tests: secret not in the URL, token error carries no secret, public route never echoes str(e), 401 drops the cached token. RED 4 failed -> GREEN.
3. _safe_next_path: added `re.fullmatch(r"/(?![/\\])[\x21-\x7e]*")`; the old refusals are kept. Test: /%09/evil, /%0a/evil, /\t/evil, \r, space, NUL. RED -> GREEN.
4. points_cloudbot: `_check_name` (`[A-Za-z0-9_@.-]+`) runs in try_spend, subtract_points and grant_points before any chat line is sent, and raises UnsafeUsername. points.py maps it to `UnusableName` (a subclass of UnknownUser, so any caller I did not touch still charges nothing). Roulette trigger and vote, and !song, answer "Your name can't be used with points (letters, digits, _ @ . - only) - nothing was charged". Tests in test_points_cloudbot and test_roulette.
5. New config key `cloudbot_reply_usernames`: [] in config.example.json, plus a FIELDS entry in "Points (Cloudbot)" with the label and description you specified. When the list is set, handle_chat_event ignores a reply unless the sender's username or display_name matches (case-insensitive, a leading @ is stripped). Test included.
6. streamlabs_oauth callback: stops the listener as before, then starts it only when `streamlabs_tips_listener_enabled` is on (same default False as main.py). The existing test now sets the flag, and a new test covers the switched-off case.
7. roulette.trigger_roulette: after a paid spend, it re-checks is_active and the cooldown inside `_spend_lock`. The loser is refunded and told "A roulette is already in progress - your points are back". `_open_session` cancels any previous pending `_end_task` (never the current task). Concurrency test: one charged, one refunded, weights and end task intact.
8. `_start_forced_buy` and `clear_forced_buy` call `_cancel_forced_buy_task()`, which now skips the current task, because the clear timer calls clear_forced_buy itself. Tests: the stale timer is cancelled, and the clear timer still broadcasts the clear.
9. widget_hub.broadcast: sends over a list() copy, all clients at once (gather), each send wrapped in `wait_for` (SEND_TIMEOUT_SECONDS=5). A client that fails or times out is dropped and closed. Client frames are logged at debug. New tests/test_widget_hub.py.
10. logger: one shared RotatingFileHandler (5 MB x 5) and stdout handler for all subsystems; the format uses `%(name)s`, which is the subsystem. `DUALBLADEX_LOG_DIR` env, which conftest sets to the temp dir. /api/logs uses `tail_lines()` (reads backwards from the end) through asyncio.to_thread. New tests/test_logger.py. I checked the real logs/backend.log after the full run: it was not modified.
11. New `config.atomic_write_text` (tmp + fsync/F_FULLFSYNC + os.replace). Config.save, greeter._save and discord_live._save all use it. greeter.remember only writes when the name is new. Tests in test_greeter and test_discord_live.
12. start_iteration_upload: `_ensure_tape_open` runs before and after the awaits. It checks for a project delete, a live tape_files delete, and refuses "This tape's files were deleted" when files_deleted_at is set. Tests: a delete requested during the awaits, and the deleted state.
13. vod_ready: a failed share is logged as a warning and the VOD still completes. Test included.
14. media_link: refuses with 409 "This file is not available" unless the status is ready or deleting. A DriveError with status 404 on get_file becomes 404 "This file is no longer on Drive". New `svc.Unavailable`, mapped in editor_api.map_errors (uploader_api uses the same mapping). DriveNotConnected is still 503. API test covers all three.
15. download_project: makes no Drive call and hands out no token. It records the editor's click and returns `{"ok": true}`, and still refuses with 400 "No file to open yet" when there is no vod_file_id. I confirmed that Site lib/editorApi.ts `recordDownload` ignores the body; Site was not edited. The test makes Drive fail and the click is still recorded.
16. notifier._ready skips projects that are soft-deleted (deleted_at), have vod_status deleting/deleted, or have a live project delete. `deletions._c()` was added to the schema-first block in run(). Test included.
17. start_over requires the iteration to be the latest READY version (`previous_ready_iteration(tape, 2**62)`). Test included.
18. `es.create_project(meta_key=...)` sets the meta key inside its own transaction (meta DDL is ensured first), and uploader_api uses it. Test: if the meta write crashes, no project is left behind.
19. `by_client_id(table, cid, author, target_id)`. add_comment and add_reply use `_repeat()`: a client_id owned by another author or another iteration/comment raises `ClientIdTaken`, which the API answers as 409 "client_id already used". Tests at the comments level and the API level.
20. search joins projects and skips rows with deleted_at set. Test included.
21. hmac.compare_digest for the OCR agent secret. health_checks: `str(config.get(...) or "")` handles a null public_base_url, and body.get moved inside the try (a non-dict body counts as a failed check). Twitch token dropped on 401. 15 s ClientTimeout on the auth (GitHub), spotify_oauth, streamlabs_oauth and streamlabs_socket sessions. valorant_rank: a bare "!" no longer crashes. _await_reply removes its waiter when the send raises. http_port now takes whole numbers 1024-65535 only, via a new `"integer"` extra in `_check_type`. Tests for each except the timeouts.
22. roulette_agent_command_users entries are now `platform:name`, and a bare name matches Twitch only (the default owner is a Twitch login). The FIELDS description says this. Tests included.

## Test summary
Full suite: 12 failed, 921 passed. The 12 failures are the known tests/test_credit_ocr.py tesseract ones. The baseline before my changes was 12 failed, 878 passed.

## Concerns / decisions
- I changed two existing tests because they pinned the old behaviour:
  - test_editor_api `test_start_over_only_on_the_latest_version` expected start-over on an uploading v2 (the M8 bug itself).
  - test_config `test_a_crash_mid_save_leaves_the_old_file_intact` patched json.dump; it now forces a failure in os.fsync, which is a real mid-write crash.
- I gave streamerbot_client's long-lived WebSocket session (streamerbot_client.py:381) no ClientTimeout: a `total` timeout would cut the live socket. editor/gdrive.py is outside core/ and stream/, so I left it alone.
- A YouTube viewer whose display name has a space can no longer spend points. This is deliberate (item 4): they are told so and nothing is charged.
- `cloudbot_reply_usernames` defaults to empty (accept any sender), as you specified. Spoofed replies stay possible until the streamer fills it in, so it should be set on the Mac Mini.
- download_project now returns {"ok": true}. Any other client that read a token from it would break; only Site's recordDownload calls it, and it ignores the body.

## Review follow-ups (commit below, same branch)
1. Safe names: allowed now are any Unicode letters (L), digits (N) and combining marks (M), plus `_ @ . -`. I did not use the suggested `[\w@.-]+`: Devanagari vowel signs (e.g. ा, ्) are combining marks, which `\w` does not match, so "राहुल_गेमर" would still have been refused. Whitespace, NBSP, zero-width space and punctuation are still refused. Test: Devanagari, accented and `@Zoë-99` names pass; "victim 99999" and others are refused. RED 3 -> GREEN.
2. The previous `_end_task` is cancelled at the top of `_open_session`, before any await. New test test_the_old_end_timer_is_cancelled_before_the_first_await checks this at the first broadcast. RED -> GREEN.
3. The losing trigger's refund now runs after `_spend_lock` is released. The test checks the lock is free during the refund.
4. TestAuditRaces: a vote now lands on the first session while the second trigger is still paying. The test asserts the vote survives, `_end_task` is still the first session's timer, and it was not cancelled. This fails against the pre-fix code.
5. widget_hub keeps each close task in a module set `_closing` and removes it when it finishes. Test included. RED -> GREEN.
Touched tests: 231 passed. Full suite: 12 failed (the known tesseract ones), 926 passed.
f755ccb
