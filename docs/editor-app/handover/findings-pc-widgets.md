# Gaming PC + widgets audit
I1 widgets/rank_session_tracker.html renderImmediateFromCache (~320) + resetSession (~470): fresh session starts from cached rank, so RR changes between streams show as session delta (+35 before any game) and an animation plays. Fix: fresh (non-resumed) session sets startAbsRR/startRR/startTier null; first fetch sets rank without animating. Change test scenario 5 to fetch a different RR (scratch: tmp/5b-audit/gp/rank-session-tracker.test.mjs).
M2 gaming pc/drive_upload.py:41,62 + uploader.py:235-247 _retry retries permanent Google 4xx (e.g. 403 storageQuotaExceeded) forever, tray stuck at %; treat 4xx except 408/429 as final with tray message.
M3 uploader.py:64-75 Windows PermissionError on os.replace/unlink when another process reads job file -> job dropped / drain stops. Retry a few times; don't drop a job for its own file OSError. (unverified)
M4 gaming pc/test_agent.py:33-45 stale test (mock returns full screen; grab_region passes bbox). Fix test: mock returns bbox-sized image, assert bbox.
M5 agent.py main loop: grab_region OSError (UAC/lock screen) kills main(). Wrap, release slot, skip tick.
M6 agent.py on_b_down: trigger() before buy_phase_id += 1; bump id first / under one lock.
M7 uploader.py:126 stream_date from st_mtime (plan says so); st_ctime would match stream start better on Windows.
M8 widgets/horizontal-chat.js truncation before emote positions; code points vs UTF-16. (unverified)
M9 rank_session_tracker.html lastSeenAt only on successful poll; 30 min resume window vs breaks up to 180 min; fetch has no timeout.
M10 mac mini/Stream/stream/widget_hub.py:60 broadcast iterates self._clients.items() while awaiting -> "dictionary changed size during iteration". Use list(...).
Known: HenrikDev key plaintext in widget; agent_config.json secret no .gitignore; forced-buy badge blank on reload.
