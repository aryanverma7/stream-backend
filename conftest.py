"""
Shared test setup. Every module in this backend that imports `config`
triggers the module-level `config = Config()` singleton to load a real
config.json AT IMPORT TIME - meaning any test file, even one that never
directly touches config.py, fails at collection if config.json doesn't
exist on disk.

This fixture creates a minimal dummy config.json before test collection
begins, so imports succeed everywhere. Individual tests then monkeypatch
`config._data` directly to set whatever values that specific test needs -
this is deliberately the SAME lightweight pattern used throughout the
plan's later tasks, rather than constructing separate Config instances,
which is what caused the failure this conftest fixes.
"""
import json
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_CREATED_DUMMY = False

if not CONFIG_PATH.exists():
    CONFIG_PATH.write_text(json.dumps({
        "http_host": "0.0.0.0",
        "http_port": 8765,
    }))
    _CREATED_DUMMY = True


def pytest_sessionfinish(session, exitstatus):
    """Clean up the dummy file after the whole test run, but only if we created it -
    never delete a real config.json a developer might actually have in place."""
    if _CREATED_DUMMY and CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
