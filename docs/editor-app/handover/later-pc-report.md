# later-pc report
Changed (gaming pc/): uploader.py, test_uploader.py, test_agent.py. UPLOADER_SETUP.md has no date text, unchanged.
1. 403 rateLimitExceeded/userRateLimitExceeded retried (uploader._rate_limited).
2. Job save failure after 5 retries kept in memory, retried at next chunk (run_job save()).
3. stream_date from st_birthtime, else st_ctime on Windows, else st_mtime (_started_at).
4. Main-loop test: failed grab releases pending slot; M6 test uses real BurstTimer subclass.
Tests: py3.12 via uv, 164 passed. Mutation-checked: removing the fixes for 1, 2 and slot release fails/hangs the new tests.
