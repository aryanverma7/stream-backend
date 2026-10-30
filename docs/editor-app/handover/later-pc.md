# Gaming PC "Later" batch. Folder /home/aryan/projects/gaming pc (no git). Tests as in fix-pc.md (uv, python 3.12). Never print agent_config.json.
1. Uploader: a Google 403 whose JSON reason is rateLimitExceeded/userRateLimitExceeded is retried with backoff, not final. Test.
2. Uploader: if a job file stays locked after the 5 retries, keep the job in memory and retry the save on the next loop instead of dropping it ("never drop a job for its own file"). Test.
3. stream_date: use the file's creation time (st_birthtime when present, else st_ctime on Windows, else st_mtime) so a stream crossing midnight is filed under its start date. Update UPLOADER_SETUP.md if it mentions the date. Test.
4. Tests for: the 15 s fetch timeout in the rank tracker is widgets (skip here); add a main-loop test that try_grab returning None releases the pending slot; make the M6 buy-phase test use the real BurstTimer.
Report: /home/aryan/.claude/jobs/48c13ad5/tmp/5b-audit/later-pc-report.md (list changed files).
