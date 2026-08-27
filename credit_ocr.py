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
one-off misread while otherwise passing both checks above. The last few
valid readings are kept in a rolling window to guard against that - but
the reported value is the MINIMUM of that window, not a majority vote
(an earlier version of this file used majority vote and got this wrong
in real testing: within one buy-phase burst, "min next round" only ever
goes DOWN as you spend more, never up, so the several readings from
before your last purchase are legitimately stale, not "more correct"
just because there are more of them - a real captured burst read 4200
four times, then 3400 once, then 2400 once right before the menu
closed, and majority vote reported the stale 4200 even though 2400 was
the genuinely correct final answer). Taking the minimum instead
correctly tracks a monotonically-decreasing value, and still rejects an
upward misread like a stray "9999" among consistent "4900"s, since an
increase can never be the true minimum. The real remaining risk this
does NOT protect against: a single misread that comes out anomalously
LOW would incorrectly become the new floor for as long as it stays in
the window - the window was shrunk from 8 to 5 and then to 4 readings
specifically to limit how long such a bad reading can linger before
rolling off.

Real-world finding #4: the number is prefixed on screen by Valorant's own
credit glyph (a custom icon, closest standard codepoint U+00A4), and
Tesseract intermittently reads that glyph as a leading 1 - a real 4200
coming back as 14200. This is finding #1 all over again: an unwhitelisted
character is not skipped, it is forced into the nearest character the
whitelist DOES allow. Two changes, one addressing the cause and one the
symptom, because the cause fix is not guaranteed to hold for a custom
game glyph the stock `eng` model was never trained on:
  1. The glyph is now in the whitelist, so Tesseract can output it as
     itself and have it stripped as punctuation instead of guessing.
  2. Values above Valorant's own 9000 credit cap are rejected outright.
     Every reading inflated by a leading 1 lands above the cap, so this
     catches the whole class regardless of what the model does.
  The case this deliberately does NOT catch: a true value under 1000, where
  the leading 1 produces a number that is still under the cap and still a
  multiple of 10 (a real 900 read as 1900). Nothing in the text alone can
  distinguish that from a genuine 1900. It survives in practice only
  because the misread is intermittent and the consensus takes the MINIMUM
  of the window, so any clean frame in the same burst wins - but if this
  ever shows up as a systematic +10000 or +1000 offset rather than an
  occasional one, the next lever is upscaling the crop before OCR, not
  more validation.

Real-world finding #5: this handler used to call Tesseract directly, and
Tesseract is a blocking subprocess call, so every capture froze the whole
backend for its duration - one asyncio process serves the dashboard, the
widget websockets and the chat handlers too. At four captures a second
during a buy phase the event loop spent most of the round blocked, which
is why the admin dashboard felt dead exactly when something was happening
and why raising the agent's capture rate did nothing: the real ceiling
was here, not on the gaming PC. OCR now runs in a small thread pool and
the handler awaits it, so the loop stays free.

The pool is deliberately small. Tesseract is CPU-bound and this is a 2012
Mac Mini; more workers than cores turns parallelism into contention. Two
workers do mean two readings can finish out of the order they were sent
in, which is harmless for a consensus that takes a minimum - the value
does not depend on order - and only very slightly changes which readings
happen to be in the window at the moment it is read.

Real-world finding #6: the reading history is cleared at the start of
every buy phase, which is correct for the consensus and terrible for
telling whether any of this works at all. A cleared window is
indistinguishable from a pipeline that has never once succeeded, and both
render as "No reading yet" on the dashboard. The last accepted reading is
therefore also kept on its own, outside the window and untouched by a
reset, purely so the panel can say "nothing this phase, but ¤3900 four
minutes ago". It is never consulted by the consensus or the roulette -
a stale value must not decide what a viewer is allowed to vote for.

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
import asyncio
import re
import io
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import pytesseract
from PIL import Image
from aiohttp import web

import ocr_agent
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


def tesseract_available() -> bool:
    """
    Whether the binary the loop above settled on actually exists.

    Surfaced on the status panel rather than left to be discovered at the
    worst moment: with tesseract missing, the agent runs, the network path
    works, and every single capture comes back 503 - so from the gaming PC
    the symptom is indistinguishable from a badly calibrated region, and
    from the dashboard it would otherwise be invisible entirely.
    """
    return os.path.exists(pytesseract.pytesseract.tesseract_cmd)

# Wider than a digit-only whitelist, deliberately - needs to recognize the
# "MIN NEXT ROUND" label text alongside the number itself, per finding #2
# above. Verified this doesn't hurt digit recognition at all; see
# test_credit_ocr.py's comma and multi-digit tests.
#
# The currency glyph is whitelisted for the same reason the comma is
# (finding #4): a character Tesseract is not allowed to output does not
# get dropped, it gets forced into the nearest character that IS allowed,
# and for this glyph that is the digit 1. Allowing it means Tesseract can
# emit it as itself, after which the digit-stripping regex below removes
# it harmlessly. U+00A4 is the closest standard codepoint to Valorant's
# own icon; whether the stock `eng` model can actually produce it for a
# custom game glyph is not guaranteed, which is why the value cap below
# exists as an independent second defense rather than a nicety.
_TESSERACT_CONFIG = "--psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789,¤ :"

_EXPECTED_LABEL = "MINNEXTROUND"  # normalized (spaces removed, uppercased) - see _contains_expected_label()

# Rolling window of the last few valid readings, for the minimum-value
# consensus (finding #3). A plain deque, not persisted to config.json -
# this is transient per-round state, not a setting.
#
# Sized at 10. The number is meaningless on its own: what is actually
# being chosen here is a DURATION, and the window is measured in readings,
# so it only means what it means at a given capture rate. The target is
# ~1 second of history - long enough to span the gap between your last
# purchase and closing the menu, short enough that it cannot reach back
# into spending you have already superseded, and short enough that a
# stray anomalously-LOW misread rolls off quickly instead of holding the
# minimum down (finding #3's one remaining weakness).
#
# It was 4 when the agent captured 4 images a second, and is 10 now that
# it captures 10 (agent.py's fix #9). Same second of history, same
# behaviour; changing agent.CAPTURE_INTERVAL_WITHIN_BURST without changing
# this silently changes how far back the consensus reaches.
#
# Note that 422s ("no number found") are never appended, so the window
# always holds the last 4 VALID readings no matter how many blank frames
# follow them - closing the buy menu can't flush the real answer out.
# Valorant hard-caps a player's credits at 9000, so "min next round" - a
# projection of next round's balance - can never legitimately exceed it
# either. Any larger value is a misread by definition, which makes this a
# cheap, absolute check that does not depend on Tesseract behaving.
_MAX_CREDITS = 9000

_READING_HISTORY_SIZE = 10
_recent_readings: deque = deque(maxlen=_READING_HISTORY_SIZE)

# The last reading that was ever accepted, and when. Deliberately NOT part
# of the rolling window and deliberately NOT cleared by handle_reset() -
# see finding #6. This exists so the dashboard can tell "the OCR pipeline
# has never worked" apart from "this buy phase produced nothing yet",
# which the window alone renders identically. Nothing that makes a
# decision reads it.
_last_reading: "int | None" = None
_last_reading_at: "float | None" = None

# Tesseract is a blocking subprocess call and this backend is a single
# asyncio process (finding #5), so OCR runs here rather than on the event
# loop. Two workers, not more: the work is CPU-bound and the machine is a
# 2012 Mac Mini, where extra workers buy contention rather than
# throughput.
_OCR_WORKERS = 2
_ocr_executor = ThreadPoolExecutor(max_workers=_OCR_WORKERS, thread_name_prefix="ocr")


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

    # Real-world finding #4: the currency glyph in front of the number is
    # a custom Valorant icon, and Tesseract intermittently reads it as a
    # leading 1 - turning a real 4200 into 14200. Every such reading is
    # above the game's own 9000 cap, so the cap catches the whole class
    # outright. Deliberately DISCARDED rather than repaired by stripping
    # the leading digit: the consensus takes the minimum of the window, so
    # a repaired-but-wrong value would become the floor and stay wrong for
    # the rest of the burst, whereas discarding costs nothing while the
    # misread is intermittent - the clean frames in the same window still
    # supply the answer.
    if value > _MAX_CREDITS:
        log.warning(f"Detected {value}, which is above Valorant's {_MAX_CREDITS} credit cap - almost certainly the "
                    f"currency glyph being read as a leading 1 (raw output: {raw_text!r}). Discarding.")
        return None

    # Real-world fix, not a guess: Valorant's entire economy (starting
    # credits, loss bonuses, weapon/ability/shield costs) operates in
    # multiples of 10 - there is no valid "min next round" value that
    # isn't. Not a perfect filter on its own - a stray misread COULD
    # coincidentally be a multiple of 10 - which is exactly why this is
    # paired with the label check above and the minimum-value consensus
    # below, rather than relied on alone.
    if value % 10 != 0:
        log.warning(f"Detected {value}, which isn't a multiple of 10 - Valorant credit values always are, "
                    f"so this is very likely a misread despite finding the expected label. Discarding.")
        return None

    return value


def get_predicted_credits() -> "int | None":
    """
    Returns the minimum value across the recent reading history (the last
    _READING_HISTORY_SIZE valid readings), not just the single latest one -
    "min next round" only ever decreases as you spend during a single
    buy-phase burst, so the smallest validated
    reading is the closest approximation of the true final amount,
    regardless of how many earlier (larger, now-stale) readings sit
    alongside it in the window. See finding #3 above for the real
    majority-vote failure this replaced. With no readings yet, returns
    None.

    Consumed by roulette.trigger_roulette(), which reads this ONCE per
    session to decide which weapons are votable. Returning None matters as
    much as returning a number there: roulette treats it as "no filter",
    opening the full roster rather than a short one, so OCR being down
    degrades the wheel's accuracy and never its availability.
    """
    if not _recent_readings:
        return None
    return min(_recent_readings)


def recent_readings() -> list:
    """
    A copy of the current rolling window, oldest first - read-only view for
    the admin dashboard's status panel, which shows how many valid readings
    the prediction is actually standing on. Copied rather than handing out
    the deque itself so a caller can't mutate the consensus history.
    """
    return list(_recent_readings)


def last_reading() -> dict:
    """
    The last reading ever accepted and how long ago it arrived, for the
    dashboard only (finding #6). Survives handle_reset(), which is the
    entire point: an empty rolling window means "nothing yet this buy
    phase" and an empty history here means "this has never worked", and
    those two need completely different things done about them.

    Never consulted by get_predicted_credits() or by the roulette. A
    reading from two rounds ago is not a budget.
    """
    if _last_reading is None or _last_reading_at is None:
        return {"credits": None, "age_seconds": None}
    return {"credits": _last_reading, "age_seconds": round(time.time() - _last_reading_at, 1)}


def forget_last_reading() -> None:
    """Clears the dashboard-only history above. Exists for the tests."""
    global _last_reading, _last_reading_at
    _last_reading = None
    _last_reading_at = None


def _remember_last_reading(value: int) -> None:
    global _last_reading, _last_reading_at
    _last_reading = value
    _last_reading_at = time.time()


def _agent_secret_ok(request: web.Request) -> bool:
    """
    The shared-secret check every agent-facing route runs. Shared between
    all three of them deliberately: these routes are in auth.py's
    open_paths, so this IS their authentication, and three copies of it
    would be three chances for one to drift.
    """
    expected_secret = config.get("ocr_agent_secret", "")
    provided_secret = request.headers.get("X-Agent-Secret", "")
    return bool(expected_secret) and provided_secret == expected_secret


async def handle_heartbeat(request: web.Request) -> web.Response:
    """
    POST /api/ocr/heartbeat - the agent saying it is still running.

    Deliberately separate from the capture route. The agent only sends
    captures while a burst is in progress, so their absence says nothing
    at all about whether it is alive, and the dashboard needs an answer to
    that question before a stream starts rather than after the first
    !roulette fails to find a budget.
    """
    if not _agent_secret_ok(request):
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    ocr_agent.record_heartbeat()
    return web.json_response({"status": "ok", "tesseract_available": tesseract_available()})


async def handle_credit_report(request: web.Request) -> web.Response:
    """POST /api/ocr/credit-report - receives a cropped screenshot from the gaming-PC agent."""
    if not _agent_secret_ok(request):
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    image_bytes = await request.read()
    if not image_bytes:
        return web.json_response({"error": "No image data in request body"}, status=400)

    # Off the event loop, not on it (finding #5). Tesseract blocks for as
    # long as it takes, and everything else this process does - the
    # dashboard, the widget sockets, the chat handlers - shares that loop.
    try:
        detected = await asyncio.get_running_loop().run_in_executor(
            _ocr_executor, extract_credits, image_bytes
        )
    except TesseractUnavailableError as e:
        return web.json_response({"error": str(e)}, status=503)

    ocr_agent.record_capture(accepted=detected is not None)

    if detected is None:
        return web.json_response({"error": "Could not validate a real reading in the captured region"}, status=422)

    _recent_readings.append(detected)
    _remember_last_reading(detected)
    consensus = get_predicted_credits()
    log.info(f"Detected {detected} (this reading) - current minimum-value consensus: {consensus}")
    return web.json_response({"credits": detected, "consensus": consensus})


async def handle_reset(request: web.Request) -> web.Response:
    """
    POST /api/ocr/reset - clears the reading history. Real bug fix: the
    agent calls this when a buy phase OPENS - without it, the previous
    round's readings were still sitting in the window, contaminating the
    new round's consensus until enough fresh readings pushed them out.

    Opening is the operative word. This used to fire on the press that
    CLOSED the menu as well, since B is the same key for both and the
    agent could not tell them apart, so finishing a buy phase wiped the
    readings that phase had just produced. See burst_timer.py's fix #8 on
    the gaming PC for the other end of that.
    """
    if not _agent_secret_ok(request):
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    _recent_readings.clear()
    # _last_reading is deliberately left alone - it is the dashboard's only
    # way to distinguish this from a pipeline that has never worked. See
    # last_reading() and finding #6.
    log.info("Reading history cleared - a new buy phase has started")
    return web.json_response({"status": "ok"})
