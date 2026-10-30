# fix-pc report
Tests: gaming pc 158 passed (uv, python 3.12, requests/pytest/Pillow/keyboard). Widgets: rank-session-tracker, roulette-overlay (17), horizontal-chat, break-screen, spotify all pass.
RED/GREEN: M2 and M3 tests verified RED by disabling the fix (permanent-4xx test hangs in infinite retry; lock test fails). M5/M6 tests reference new symbols (try_grab, BuyPhase) so were RED by AttributeError; M6 test also fails without the lock by construction. I1/M9/M8 tests were written alongside the fix; the I1 scenario-5 and M9 failed-poll cases failed before the fix logic was in place (-15 / early session reset) during development, not re-run against the original file.

## Copy to pc-ocr repo (gaming pc/)
- agent.py (try_grab, BuyPhase; main loop uses them)
- uploader.py (_io_retry for replace/unlink, Google 4xx final)
- test_agent.py, test_uploader.py
## Re-copy to OBS machine (widgets/)
- rank_session_tracker.html (I1, M9)
- horizontal-chat.js (M8)
## Tests only (no OBS copy)
- widgets/rank-session-tracker.test.mjs, widgets/horizontal-chat.test.mjs
Not touched: M7, M10 (other area), agent_config.json, Henrik key.
