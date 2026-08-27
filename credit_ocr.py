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
rolling off. Finding #7 below replaces the minimum outright, and finding
#8 settles what happens to that anomalously low reading: it is trusted,
because it cannot be told apart from a real purchase and the cost of
disbelieving a real purchase is higher. The reasoning here is kept
because it is still the reason a rise is not taken on one sighting.

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
  because the misread is intermittent and inflating, and an inflating
  value is the one direction the consensus still refuses on a single
  sighting (findings #7 and #8), so the clean frames in the same burst
  win - but if this ever shows up as a systematic +10000 or +1000 offset
  rather than an occasional one, the next lever is upscaling the crop
  before OCR, not more validation.

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
in, so the newest entry in the window is not always the newest capture.
That mattered not at all to a consensus that took a minimum, and it does
matter to one that prefers the latest - which is why finding #7 anchors
on a corroborated value rather than simply taking the last element. The
straggler this actually protects against is the pre-purchase one, which
arrives HIGHER than the value that replaced it, and an unconfirmed rise
is precisely what finding #8 still refuses.

Real-world finding #6: the reading history is cleared at the start of
every buy phase, which is correct for the consensus and terrible for
telling whether any of this works at all. A cleared window is
indistinguishable from a pipeline that has never once succeeded, and both
render as "No reading yet" on the dashboard. The last accepted reading is
therefore also kept on its own, outside the window and untouched by a
reset, purely so the panel can say "nothing this phase, but ¤3900 four
minutes ago". It is never consulted by the consensus or the roulette -
a stale value must not decide what a viewer is allowed to vote for.

Real-world finding #7, from watching how the buy menu is actually
driven. The menu is opened with B and closed with Esc, and one buy phase
routinely involves opening it more than once - buy a rifle, close, think,
re-open to add armour. The gaming PC now keeps both looks' readings in
one window rather than resetting between them (see burst_timer.py's fix
#10 there), which is only useful if the newer look wins, and under a
minimum it did not: the pre-purchase readings of the FIRST look are
higher, so the minimum still tracked the right thing while credits only
fell - but a refund inside the buy phase raises "min next round", and no
minimum can ever follow a value upward.

The consensus is therefore anchored on the first value to be seen
_CORROBORATING_READINGS times while scanning BACK from the newest
reading, rather than on the plain smallest. Counting only what has been
scanned so far, rather than the whole window, is what makes this survive
the out-of-order completion described under finding #5: a stale straggler
that lands last is a single sighting at the point it is reached, and the
older readings that agree with it are behind it and never counted.
Finding #8 below narrows where that corroboration requirement applies.

Real-world finding #8, from a real log of a fast buy:

    OK - this reading: 6200 | current consensus: 6200   (x5)
    OK - this reading: 3300 | current consensus: 6200
    422 - no number found in the captured region.       (x15)

The purchase landed, the menu was closed immediately after it, and the
reported consensus stayed on the pre-purchase 6200 - the single worst
answer the window could have given, since it tells the roulette the
streamer can afford a gun they have just spent the credits for.

Requiring corroboration in BOTH directions was the mistake. The value
that matters most is the last one before the menu closes, and that is
exactly the value with the fewest chances to be read twice: at ten
captures a second, a purchase confirmed a fifth of a second before Esc
gets one or two frames and no more. So a rule that ignores anything seen
once systematically discards the final purchase of every quick buy - not
occasionally, but every time - to guard against a misread that is rare.

The two directions are not symmetrical, and that is what the rule now
uses:

  A reading LOWER than the corroborated value is what spending looks
  like, which is the normal, expected motion of "min next round" within
  a buy phase. It is taken immediately, uncorroborated.

  A reading HIGHER than the corroborated value is either a refund - much
  rarer, and one you keep shopping after, so the next frame corroborates
  it within a tenth of a second - or a misread of the classic inflating
  kind (finding #4's leading 1, a high digit substitution). It still
  needs a second sighting.

The error each choice makes when it is wrong also points the same way. An
under-estimate shrinks the votable roster: viewers get fewer options for
one session. An over-estimate offers weapons the streamer cannot buy,
which is the exact failure this whole feature exists to prevent. Erring
low is the cheap side.

What this deliberately does NOT protect against, stated plainly because
finding #3 tried to and could not: a dropped-digit misread (a real 2400
read as 240) arriving as the very last valid frame before the menu
closes is trusted. There is nothing in the data to separate it from a
genuine purchase - both are a single low reading with nothing after it.
The guards that would catch it (reject a value that is exactly a tenth of
the corroborated one, reject a value that is the corroborated one with a
digit deleted) each reject real buys too - 4000 credits spent down to 400
is an Odin and light shields - and rejecting a real buy reproduces the
bug above, so no guard is worth its false positives here.

Two looks at one buy phase is not the same as two rounds, and the
difference is time. Readings older than _READING_MAX_AGE_SECONDS are
dropped from the consensus here, mirroring the gap the agent uses to
decide when to reset at all, so a reset POST that never arrives cannot
leave a previous round's budget standing.

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
from collections import Counter, deque
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

# Valorant hard-caps a player's credits at 9000, so "min next round" - a
# projection of next round's balance - can never legitimately exceed it
# either. Any larger value is a misread by definition, which makes this a
# cheap, absolute check that does not depend on Tesseract behaving.
_MAX_CREDITS = 9000

# Rolling window of the last few valid readings, which the consensus
# (finding #7) reads newest-first. A plain deque, not persisted to
# config.json - this is transient per-buy-phase state, not a setting.
#
# Sized at 10. The number is meaningless on its own: what is actually
# being chosen here is a DURATION, and the window is measured in readings,
# so it only means what it means at a given capture rate. The target is
# ~1 second of history - long enough that a value which is genuinely on
# screen gets read many times over and so is corroborated immediately,
# short enough that the window turns over within a fraction of the time
# it takes to make a purchase, so the answer follows what the menu shows
# rather than lagging it.
#
# It was 4 when the agent captured 4 images a second, and is 10 now that
# it captures 10 (agent.py's fix #9). Same second of history, same
# behaviour; changing agent.CAPTURE_INTERVAL_WITHIN_BURST without changing
# this silently changes how far back the consensus reaches.
#
# Note that 422s ("no number found") are never appended, so the window
# always holds the last 10 VALID readings no matter how many blank frames
# follow them - closing the buy menu can't flush the real answer out.
_READING_HISTORY_SIZE = 10
_recent_readings: deque = deque(maxlen=_READING_HISTORY_SIZE)

# How many times a value has to be seen, scanning back from the newest
# reading, for it to become the anchor the newest reading is judged
# against. Two, not more: at ten captures a second anything really on
# screen is read again within a tenth of a second, so this costs
# essentially no lag.
#
# Since finding #8 this gates only an UPWARD move away from that anchor.
# A downward one is taken on a single sighting, because the last reading
# before the menu closes is both the one that matters most and the one
# least likely to ever be read twice.
_CORROBORATING_READINGS = 2

# When the newest reading in the window is older than this, the window is
# treated as empty rather than as an answer. The agent already resets the
# history at the start of a new buy phase, so this never fires in normal
# operation - it exists for when that reset does NOT arrive: a dropped
# POST, an agent restarted mid-match, a gaming PC that went to sleep. The
# failure it prevents is the expensive one, a previous round's budget
# quietly deciding what viewers may vote for.
#
# The same number as burst_timer.NEW_ROUND_GAP_SECONDS on the gaming PC,
# because it is the same fact stated from the other side: readings more
# than twenty seconds apart belong to different rounds. Change one and
# change the other.
_READING_MAX_AGE_SECONDS = 20

# When the newest entry in the window arrived. One timestamp for the whole
# window rather than one per reading, because the window only ever spans
# about a second of wall clock - if the newest reading is fresh then none
# of them is stale, and if the newest is stale then all of them are.
_window_last_append_at: "float | None" = None

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
    # the leading digit: a repaired value is indistinguishable from a real
    # one, so several of them in a row would corroborate each other and
    # become the consensus, whereas discarding costs nothing while the
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
    # paired with the label check above and the corroborated consensus
    # below, rather than relied on alone.
    if value % 10 != 0:
        log.warning(f"Detected {value}, which isn't a multiple of 10 - Valorant credit values always are, "
                    f"so this is very likely a misread despite finding the expected label. Discarding.")
        return None

    return value


def _window_is_stale() -> bool:
    """
    Whether the newest reading in the window is old enough to belong to a
    different round - see _READING_MAX_AGE_SECONDS. A stale window is
    reported as no window at all rather than as an old answer.
    """
    if _window_last_append_at is None:
        return True
    return (time.time() - _window_last_append_at) > _READING_MAX_AGE_SECONDS


def _corroborated_value() -> "int | None":
    """
    The newest value the window has actually confirmed: the first one seen
    _CORROBORATING_READINGS times while scanning BACK from the newest
    reading. None while the window is still too short for anything to have
    repeated.

    The scan-so-far count is load-bearing and not the same as counting the
    whole window. A reading that finished out of order (finding #5) lands
    at the end of the window holding a value from BEFORE the purchase, and
    the earlier readings that agree with it are older than it - counting
    the whole window would let them vouch for it and undo the purchase.
    Counting only what has been scanned reaches that straggler first, sees
    it once, and moves on.
    """
    seen = Counter()
    for value in reversed(_recent_readings):
        seen[value] += 1
        if seen[value] >= _CORROBORATING_READINGS:
            return value
    return None


def get_predicted_credits() -> "int | None":
    """
    Returns the newest reading, unless it is an unconfirmed rise above the
    value the window has corroborated - in which case the corroborated
    value stands until the rise is seen a second time. With no readings -
    or with a window too old to belong to this round - returns None.

    "Most recent" rather than "smallest" (finding #7): one buy phase now
    spans however many times the menu was opened, so the later look is the
    one that reflects what has actually been bought, and a refund can move
    the true value UP, which a minimum can never follow.

    Asymmetric rather than "most recent, corroborated" (finding #8): a
    drop is what spending looks like and is taken on a single sighting,
    because the reading that matters most - the one taken right before the
    menu closes - is also the one with the fewest chances to be read
    twice. A rise is either a refund, which you keep shopping after and so
    is corroborated a frame later anyway, or an inflating misread of
    finding #4's kind, so it waits.

    Consumed by roulette.trigger_roulette(), which reads this ONCE per
    session to decide which weapons are votable. Returning None matters as
    much as returning a number there: roulette treats it as "no filter",
    opening the full roster rather than a short one, so OCR being down
    degrades the wheel's accuracy and never its availability.
    """
    if not _recent_readings or _window_is_stale():
        return None

    newest = _recent_readings[-1]
    corroborated = _corroborated_value()

    # Nothing corroborated yet means one or two frames into a burst. The
    # newest reading is still a better answer than none: the roulette's
    # alternative to a number is the unfiltered roster.
    if corroborated is None or newest <= corroborated:
        return newest

    return corroborated


def recent_readings() -> list:
    """
    A copy of the current rolling window, oldest first - read-only view for
    the admin dashboard's status panel, which shows what the prediction is
    actually standing on. Copied rather than handing out the deque itself
    so a caller can't mutate the consensus history.

    Empty when the window is too old to count, so the panel and the
    roulette never disagree about whether there is a reading.
    """
    if _window_is_stale():
        return []
    return list(_recent_readings)


def _record_reading(value: int) -> None:
    """
    Appends to the rolling window and stamps when it happened. The only
    way anything should enter the window - the timestamp is what makes
    _window_is_stale() mean anything, and an append that skipped it would
    leave a fresh reading looking like a stale one.
    """
    global _window_last_append_at
    _recent_readings.append(value)
    _window_last_append_at = time.time()


def _clear_window() -> None:
    """Empties the rolling window. Leaves _last_reading alone - see last_reading()."""
    global _window_last_append_at
    _recent_readings.clear()
    _window_last_append_at = None


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

    _record_reading(detected)
    _remember_last_reading(detected)
    consensus = get_predicted_credits()
    log.info(f"Detected {detected} (this reading) - current consensus: {consensus}")
    return web.json_response({"credits": detected, "consensus": consensus})


async def handle_reset(request: web.Request) -> web.Response:
    """
    POST /api/ocr/reset - clears the reading history. Real bug fix: the
    agent calls this at the start of a new buy phase - without it, the
    previous round's readings were still sitting in the window,
    contaminating the new round's consensus until enough fresh readings
    pushed them out.

    "A new buy phase" is a narrower thing than "a B press", and getting
    that distinction wrong has broken this twice. It fired on the press
    that CLOSED the menu, wiping the readings the phase had just produced;
    then it fired on a re-open of the SAME buy phase, wiping the first
    look at it. The agent now decides by the gap between presses rather
    than by the press itself - see burst_timer.py's fix #10 on the gaming
    PC - and this end enforces the same gap independently via
    _READING_MAX_AGE_SECONDS, so a reset that never arrives cannot leave a
    previous round standing.
    """
    if not _agent_secret_ok(request):
        return web.json_response({"error": "Invalid or missing agent secret"}, status=401)

    _clear_window()
    # _last_reading is deliberately left alone - it is the dashboard's only
    # way to distinguish this from a pipeline that has never worked. See
    # last_reading() and finding #6.
    log.info("Reading history cleared - a new buy phase has started")
    return web.json_response({"status": "ok"})
