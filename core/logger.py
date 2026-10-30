"""
Structured, subsystem-tagged logging.

Per Task #3's design: logs go to a file (feeding the admin dashboard's future
log viewer, Task #4) AND to stdout (useful when running interactively during
development). Every log line is tagged with which subsystem produced it
(e.g. [WebSocketServer], [HTTP], [StreamerBot]) so a scrolling log file stays
readable once there are several concurrent things happening at once.
"""
import logging
import logging.handlers
import os
import sys
from pathlib import Path

# DUALBLADEX_LOG_DIR lets the test suite write somewhere other than the real logs/ (see conftest.py).
LOG_DIR = Path(os.environ.get("DUALBLADEX_LOG_DIR") or Path(__file__).parent.parent / "logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "backend.log"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 5

# One handler of each kind shared by every subsystem: a rotating handler per logger would have each
# one renaming the file under the others. The subsystem tag is the logger's name.
_formatter = logging.Formatter(fmt="%(asctime)s [%(name)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
_handlers: list[logging.Handler] = []


def _shared_handlers() -> list[logging.Handler]:
    if not _handlers:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        stream_handler = logging.StreamHandler(sys.stdout)
        for h in (file_handler, stream_handler):
            h.setFormatter(_formatter)
            _handlers.append(h)
    return _handlers


def get_logger(subsystem: str) -> logging.Logger:
    """
    Call this once per module/subsystem, e.g.:
        log = get_logger("WebSocketServer")
        log.info("Client connected")
    Produces lines like: 2026-08-18 10:15:03 [WebSocketServer] INFO Client connected
    """
    logger = logging.getLogger(subsystem)
    if logger.handlers:
        # Already configured (e.g. re-imported) - don't add duplicate handlers.
        return logger

    logger.setLevel(logging.INFO)
    for handler in _shared_handlers():
        logger.addHandler(handler)
    return logger
