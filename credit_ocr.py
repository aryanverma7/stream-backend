"""
Next-round-credit detection (Task #8) - the Mac Mini side.

Architecture, per direction: Valorant only runs on the gaming PC, so a
small agent there handles screen capture (unavoidable - the Mac Mini has
no way to see a different physical machine's screen), but the actual OCR
computation happens here, keeping the gaming PC's CPU/GPU free for the
game and stream. The agent POSTs a cropped screenshot of the calibrated
region; this module runs Tesseract on it and extracts the number.

The OCR approach below was verified against real test images before
being written up as "working," not assumed.

Real-world finding #1: Valorant formats larger numbers with a comma
thousands separator (e.g. "5,700"), and an EARLIER digit-only whitelist
forced Tesseract to misclassify that comma as the closest allowed digit
rather than reject it - corrupting the reading (a real "5,700" got
reported as 15700).

Real-world finding #2: a burst window can occasionally outlast the buy
menu itself (closed early, or a stray frame captured right as it closes),
briefly reading something else on screen (like the minimap) instead -
producing plausible-looking garbage. Two independent defenses now guard
against this:
  1. The calibrated region is expected to include the "MIN NEXT ROUND"
     label text, not just the number - gameplay content essentially
     never accidentally produces that exact phrase, so its absence is a
     strong signal this capture isn't the real thing.
  2. Valorant's entire economy (starting credits, loss bonuses, weapon/
     ability/shield costs) operates in multiples of 10 - a value that
     isn't gets discarded outright.
  Because the whitelist now needs to recognize letters too (for the label
  text), not just digits, it's wider than a digit-only one would be -
  which actually also naturally resolves finding #1, since Tesseract can
  now correctly output a comma as a comma instead of being forced to
  guess a digit for it.

Real-world finding #3: even a validated reading can occasionally be a
one-off misread while otherwise passing both checks above. Rather than
trust a single reading, the last few valid readings are kept in a
rolling window, and the reported value is whichever number appears most
often in that window - a majority vote, giving one bad reading less
power to override several consistent ones.

Requires the native `tesseract` binary installed on the Mac Mini - not a
pure-Python dependency. On this project's specific Mac Mini (2012,
Catalina), Homebrew's tesseract formula hit a real, unresolvable
Python/pip bootstrapping bug in one of its dependencies - MacPorts
(`sudo port install tesseract tesseract-eng`) installed cleanly instead,
to /opt/local/bin. Homebrew (/usr/local/bin) is also checked below, in
case a different machine uses that instead.

Auth: this endpoint is called by an unattended script on a different
machine, not a browser with a GitHub OAuth session - so it's gated by a
simple shared secret (X-Agent-Secret header, checked against
ocr_agent_secret in config.json) rather than the admin auth system. It
IS listed in auth.py's open_paths - not because it's meant to be public,
but because the gaming-PC agent calling it has no GitHub session to
provide, so this endpoint has to be exempt from that middleware for its
own shared-secret check to ever be reached at all. Confirmed this the
hard way: with it NOT in open_paths, even a request with the correct
secret got rejected with a 401 before the handler ever ran.
"""
import re
import io
import os
from collections import deque, Counter

import pytesseract
from PIL import Image
from aiohttp import web

from config import config
from logger import get_logger

log = get_logger("CreditOCR")


class TesseractUnavailableError(Exception):
    """
    Raised specifically when the tesseract binary itself can't be found -
    a Mac Mini setup problem, distinct from "this specific image had no
    readable digits." Kept as its own exception so the HTTP handler can
    give a genuinely different, more actionable response for each case,
    rather than collapsing both into the same generic error.
    """
    pass

# Explicitly pointing at the binary rather than relying on PATH - launchd
# services run with their own minimal default PATH (confirmed the hard
# way: the plist has no PATH override, and MacPorts installs tesseract to
# /opt/local/bin, which isn't on that default PATH at all). This works
# correctly whether tesseract was installed via MacPorts (/opt/local/bin)
# or Homebrew (/usr/local/bin), checking both real locations rather than
# hardcoding just one.
for candidate in ("/opt/local/bin/tesseract", "/usr/local/bin/tesseract"):
    if os.path.exists(candidate):
        pytesseract.pytesseract.tesseract_cmd = candidate
        break

# Wider than a digit-only whitelist, deliberately - needs to recognize the
# "MIN NEXT ROUND" label text alongside the number itself, per finding #2
# above. Verified this doesn't hurt digit recognition at all; see
# test_credit_ocr.py's comma and multi-digit tests.
_TESSERACT_CONFIG = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789, :"

_EXPECTED_LABEL = "MINNEXTROUND"  # normalized (spaces removed, uppercased) - see _contains_expected_label()

# Rolling window of the last few valid readings, for the majority-vote
# consensus (finding #3). A plain deque, not persisted to config.json -
# this is transient per-round state, not a setting.
# Rolling window of the last few valid readings, for the majority-vote
# consensus (finding #3). A plain deque, not persisted to config.json -
# this is transient per-round state, not a setting. Sized at 8 to match
# the agent's 4-images/second capture rate, covering roughly the same
# ~2-second real time span as the original 4-reading window did at 2/sec.
_READING_HISTORY_SIZE = 8
_recent_readings: deque = deque(maxlen=_READING_HISTORY_SIZE)


def _contains_expected_label(raw_text: str) -> bool:
    """
    Checks for "MIN NEXT ROUND" in the OCR output, ignoring spacing and
    case - Tesseract's exact spacing between words isn't reliably
    consistent (confirmed directly: a real test render came back as
    "MINNEXTROUND" with no spaces at all), so comparing on a
    space-stripped, uppercased basis is what actually matches reality.
    """
    normalized = re.sub(r"\s+", "", raw_text).upper()
    return _EXPECTED_LABEL in normalized


def extract_credits(image_bytes: bytes) -> "int | None":
    """
    Runs OCR on a cropped screenshot, returning the detected number or
    None if validation failed (no digits, missing the expected label
    text, or not a multiple of 10). Kept separate from the HTTP handler
    below so this exact logic is directly testable with real image
    bytes, without needing a running server.
    """
    try:
        image = Image.open(io.BytesIO(image_bytes))
    except Exception as e:
        log.warning(f"Could not decode the received image: {e}")
        return None

    try:
        raw_text = pytesseract.image_to_string(image, config=_TESSERACT_CONFIG)
    except pytesseract.TesseractNotFoundError:
        # The exact failure this project hit for real: launchd runs with
        # its own minimal PATH, not inheriting the interactive shell's
        # PATH from .zshrc - if tesseract genuinely isn't at either
        # location checked above, this is the failure mode, and it needs
        # to surface clearly rather than crash into an opaque 500 with
        # nothing useful in backend.log.
        log.error(
            "tesseract binary not found - checked /opt/local/bin and "
            "/usr/local/bin. If it's installed somewhere else, update "
            "the path in credit_ocr.py directly."
        )
        raise TesseractUnavailableError("tesseract binary not found on the Mac Mini")
    except Exception as e:
        log.warning(f"Tesseract OCR call failed unexpectedly: {e}")
        return None

    if not _contains_expected_label(raw_text):
        log.warning(f"OCR output didn't contain 'MIN NEXT ROUND' - likely not the buy screen "
                    f"(raw output: {raw_text!r}). Discarding.")
        return None

    digits = re.sub(r"[^\d]", "", raw_text)  # strip everything non-digit, including the now-recognized letters/comma

    if not digits:
        log.warning(f"OCR found the expected label but no digits (raw output: {raw_text!r})")
        return None

    value = int(digits)

    # Real-world fix, not a guess: Valorant's entire economy (starting
    # credits, loss bonuses, weapon/ability/shield costs) operates in
    # multiples of 10 - there is no valid "min next round" value that
    # isn't. Not a perfect filter on its own - a stray misread COULD
    # coincidentally be a multiple of 10 - which is exactly why this is
    # paired with the label check above and the majority vote below,
    # rather than relied on alone.
    if value % 10 != 0:
        log.warning(f"Detected {value}, which isn't a multiple of 10 - Valorant credit values always are, "
                    f"so this is very likely a misread despite finding the expected label. Discarding.")
        return None

    return value


def get_predicted_credits() -> "int | None":
    """
    Returns the majority-vote value across the recent reading history,
    not just the single latest one - one bad reading (that still somehow
    passed both validations above) shouldn't override several consistent
    ones. With no readings yet, returns None.

    Exposed for other modules (e.g. a future Roulette affordability
    filter) to read the current value - deliberately not wired into
    Roulette's own gun-list filtering yet, since that wasn't asked for in
    this pass.
    """
    if not _recent_readings:
        return None
    most_common_value, _count = Counter(_recent_readings).most_common(1)[0]
    return most_common_value


async def handle_credit_report(request: web.Request) -> web.Response:
    """POST /api/ocr/credit-report - receives a cropped screenshot from the gaming-PC agent."""
    expected_secret = config.get("ocr_agent_secret", "")
    provided_secret = request.headers.get("X-Agent-Secret", "")
    if not expected_secret or provided_secret != expected_secret:
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    image_bytes = await request.read()
    if not image_bytes:
        return web.json_response({"error": "No image data in request body"}, status=400)

    try:
        detected = extract_credits(image_bytes)
    except TesseractUnavailableError as e:
        return web.json_response({"error": str(e)}, status=503)

    if detected is None:
        return web.json_response({"error": "Could not validate a real reading in the captured region"}, status=422)

    _recent_readings.append(detected)
    consensus = get_predicted_credits()
    log.info(f"Detected {detected} (this reading) - current majority-vote value: {consensus}")
    return web.json_response({"credits": detected, "consensus": consensus})


async def handle_reset(request: web.Request) -> web.Response:
    """
    POST /api/ocr/reset - clears the reading history. Real bug fix: the
    agent calls this once at the start of each genuinely NEW buy phase
    (not when re-opening the same one) - without it, the previous round's
    readings were still sitting in the window, contaminating the new
    round's majority vote until enough fresh readings pushed them out.
    """
    expected_secret = config.get("ocr_agent_secret", "")
    provided_secret = request.headers.get("X-Agent-Secret", "")
    if not expected_secret or provided_secret != expected_secret:
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    _recent_readings.clear()
    log.info("Reading history cleared - a new buy phase has started")
    return web.json_response({"status": "ok"})
