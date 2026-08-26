"""
Structured, subsystem-tagged logging.

Per Task #3's design: logs go to a file (feeding the admin dashboard's future
log viewer, Task #4) AND to stdout (useful when running interactively during
development). Every log line is tagged with which subsystem produced it
(e.g. [WebSocketServer], [HTTP], [StreamerBot]) so a scrolling log file stays
readable once there are several concurrent things happening at once.
"""
import logging
import sys
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "backend.log"


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
    formatter = logging.Formatter(
        fmt=f"%(asctime)s [{subsystem}] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger
