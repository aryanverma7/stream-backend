"""
Shared test setup. Every module in this backend that imports `config`
triggers the module-level `config = Config()` singleton to load a real
config.json AT IMPORT TIME - meaning any test file, even one that never
directly touches config.py, fails at collection if config.json doesn't
exist on disk.

This creates a minimal dummy config.json in a temp folder before test
collection begins and points config.py at it, so imports succeed everywhere
and the real config.json is never read or written. Individual tests then monkeypatch
`config._data` directly to set whatever values that specific test needs -
this is deliberately the SAME lightweight pattern used throughout the
plan's later tasks, rather than constructing separate Config instances,
which is what caused the failure this conftest fixes.
"""
import json
import os
import tempfile
from pathlib import Path

# The suite NEVER uses the real config.json. Tests replace config._data and many code paths call
# config.save() (roulette.set_agent, Spotify/Drive token rotation, the Settings PUT), so a suite run
# on the Mac Mini used to overwrite its live config.json with test values - every secret and setting
# gone at the next backend restart. Set before anything imports config.
CONFIG_PATH = Path(tempfile.mkdtemp(prefix="dualbladex-tests-")) / "config.json"
CONFIG_PATH.write_text(json.dumps({"http_host": "0.0.0.0", "http_port": 8765}))
os.environ["DUALBLADEX_CONFIG"] = str(CONFIG_PATH)
# Likewise the log: tests must not write the real logs/backend.log.
os.environ["DUALBLADEX_LOG_DIR"] = str(CONFIG_PATH.parent / "logs")


# aioresponses (latest 0.7.9) predates aiohttp 3.14's required `stream_writer`
# keyword on ClientResponse and builds its fake responses without it. Supply a
# no-op default until aioresponses releases a fix; remove this then.
import aioresponses.core as _ar_core
from aiohttp import ClientResponse as _ClientResponse


class _NoWriter:
    output_size = 0


class _CompatClientResponse(_ClientResponse):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("stream_writer", _NoWriter())
        super().__init__(*args, **kwargs)


_ar_core.ClientResponse = _CompatClientResponse
