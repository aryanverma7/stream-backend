"""
Liveness tracking for the gaming PC's OCR agent.

Separate from credit_ocr.py on purpose. That module is about reading a
number out of a screenshot and needs Tesseract and Pillow to do it; this
one is about whether the machine sending those screenshots is switched on,
which is a different question with no image processing in it at all - and
keeping them apart means the liveness rules can be tested anywhere,
including on a machine with no OCR stack installed.

Why a heartbeat rather than just timestamping the captures: the agent only
POSTs while a burst is running, which is a couple of seconds per round and
nothing whatsoever between rounds. "No capture in the last minute" is the
completely normal state during a gunfight, so it cannot be used to answer
"is the agent running". The agent therefore pings on its own timer, and
that ping - not the captures - is what the dashboard's OCR row reports.

The captures are still counted, because "the agent is alive but nothing it
sends is readable" and "the agent is not running" are different problems
with different fixes, and the panel should not blur them into one light.
"""
import time

from logger import get_logger

log = get_logger("OCRAgent")

# Three missed pings at the agent's 15-second interval. Generous on
# purpose: a single dropped packet or a Wi-Fi hiccup mid-round should not
# make the dashboard claim the agent died, because the panel is only
# useful if a red light means something.
HEARTBEAT_TIMEOUT_SECONDS = 45

_last_heartbeat_at: "float | None" = None
_last_capture_at: "float | None" = None
_last_accepted_at: "float | None" = None
_captures_received = 0
_captures_accepted = 0


def record_heartbeat() -> None:
    """Called by the /api/ocr/heartbeat handler, once the agent secret checks out."""
    global _last_heartbeat_at
    first_contact = _last_heartbeat_at is None
    _last_heartbeat_at = time.time()
    if first_contact:
        # Worth one line, and only one: this is the moment the gaming PC
        # comes into view after a backend restart.
        log.info("OCR agent is reporting in")


def record_capture(accepted: bool) -> None:
    """
    Called for every authenticated capture POST, whether or not a number
    came out of it. A rejected capture still proves the agent is running
    and still proves the network path works - it only says the calibrated
    region had nothing readable in it at that instant, which is the
    expected outcome for most frames of a burst.
    """
    global _last_capture_at, _last_accepted_at, _captures_received, _captures_accepted
    now = time.time()
    _last_capture_at = now
    _captures_received += 1
    if accepted:
        _last_accepted_at = now
        _captures_accepted += 1


def _age(timestamp: "float | None", now: float) -> "float | None":
    if timestamp is None:
        return None
    return round(now - timestamp, 1)


def status() -> dict:
    """
    A snapshot for /api/status. `connected` counts a recent capture as
    proof of life alongside the heartbeat, so an older agent build that
    predates the heartbeat still shows as connected while it is actually
    sending work, rather than being reported dead mid-burst.
    """
    now = time.time()
    heartbeat_age = _age(_last_heartbeat_at, now)
    capture_age = _age(_last_capture_at, now)
    recent = [a for a in (heartbeat_age, capture_age) if a is not None]
    return {
        "connected": bool(recent) and min(recent) <= HEARTBEAT_TIMEOUT_SECONDS,
        "last_heartbeat_age_seconds": heartbeat_age,
        "last_capture_age_seconds": capture_age,
        "last_accepted_age_seconds": _age(_last_accepted_at, now),
        "captures_received": _captures_received,
        "captures_accepted": _captures_accepted,
        "heartbeat_timeout_seconds": HEARTBEAT_TIMEOUT_SECONDS,
    }


def reset() -> None:
    """Drops every recorded timestamp and counter. Exists for the tests."""
    global _last_heartbeat_at, _last_capture_at, _last_accepted_at
    global _captures_received, _captures_accepted
    _last_heartbeat_at = None
    _last_capture_at = None
    _last_accepted_at = None
    _captures_received = 0
    _captures_accepted = 0
