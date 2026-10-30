# Backend fixer context
- Repo /home/aryan/projects/mac mini/Stream, branch `later-fixes` (checked out; stacked on quick-fixes = PR #15). Commit there; never push.
- Read /home/aryan/projects/CLAUDE.md backend sections first. Rules: db.transaction() is the only multi-statement write; never hold a transaction across an await; DDL before transactions; tables created lazily; admin checks inside /api/editor handlers; new route for a non-browser client needs OPEN_PATHS; a new config key needs a config.example.json entry AND a FIELDS entry in core/config_schema.py; timing code takes `now`.
- NEVER read, print, or modify config.json / app.db*. Tests must never touch them (conftest uses DUALBLADEX_CONFIG).
- Tests: `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings` (12 known failures in tests/test_credit_ocr.py: no tesseract). Each fix gets a test that fails without it (TDD: show RED then GREEN in the report).
- Commit: `git -c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com commit`, stage files BY NAME (Zone.Identifier junk), message ends with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EBDRBtUCJgM7PK9fWR7XzN
  Several small commits grouped by theme are fine.
- No subagents. If a fix needs a decision the task file doesn't settle, take the safest option, note it in the report.
- Report: write to the report path given; reply with ONLY status, commits, one-line test summary, concerns, report path.
