import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import credit_ocr
import ocr_agent
from config import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_reading_history():
    """Every test gets a fresh reading history - this module uses shared,
    module-level state by design (a real rolling window across real
    requests), which means tests must reset it between runs."""
    credit_ocr._clear_window()
    credit_ocr.forget_last_reading()
    credit_ocr.forget_buy_phase()
    # Liveness is shared module state too, and since finding #9 the
    # consensus reads it: an agent that has been heard from and then gone
    # quiet expires the window. A leftover timestamp from another test
    # would decide that here.
    ocr_agent.reset()
    yield
    credit_ocr._clear_window()
    credit_ocr.forget_last_reading()
    credit_ocr.forget_buy_phase()
    ocr_agent.reset()


class TestExtractCredits:
    def test_extracts_a_four_digit_number_from_a_realistic_image(self):
        image_bytes = (FIXTURES / "credits_4900.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) == 4900

    def test_extracts_a_second_different_number_confirming_it_is_not_just_a_lucky_case(self):
        image_bytes = (FIXTURES / "credits_8700.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) == 8700

    def test_rejects_a_five_digit_value_above_valorant_s_own_credit_cap(self):
        """
        This fixture used to be asserted as a VALID 13100 reading, back
        when the only size-related worry was "does multi-digit OCR work at
        all". It is now the cap test instead: Valorant caps credits at
        9000, so 13100 was never a reachable balance, and the shape of it -
        a real four-digit value with an extra leading digit - is exactly
        what the credit glyph misread produces (finding #4).
        """
        image_bytes = (FIXTURES / "credits_13100.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) is None

    def test_still_correct_with_blur_and_jpeg_compression_closer_to_a_real_screen_capture(self):
        image_bytes = (FIXTURES / "credits_noisy.jpg").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) == 4900

    def test_correctly_handles_a_comma_formatted_number(self):
        """
        Guards against a real bug found after actual deployment: Valorant
        formats larger numbers with a comma thousands separator, and the
        original digit-only whitelist forced Tesseract to misclassify the
        comma as an extra digit instead of recognizing it as punctuation -
        a real "5,700" got reported as 15700.
        """
        image_bytes = (FIXTURES / "credits_5700_comma.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) == 5700

    def test_rejects_a_value_that_is_not_a_multiple_of_10(self):
        """
        The exact real garbage value observed in actual use (113, when
        the calibrated region briefly showed the minimap instead of the
        buy screen). Valorant's entire economy operates in multiples of
        10, so this is discarded rather than reported as a real reading.
        """
        image_bytes = (FIXTURES / "credits_113_not_multiple_of_10.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) is None

    def test_zero_is_correctly_accepted_as_a_valid_multiple_of_10(self):
        """
        0 % 10 == 0, so a genuine zero-balance reading (matching your own
        screenshot showing exactly this state) must NOT be incorrectly
        rejected by the same validation that catches garbage - and since
        0 is falsy in Python, this also guards against an accidental
        "if not value" style bug hiding elsewhere in the pipeline.
        """
        image_bytes = (FIXTURES / "credits_0.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) == 0

    def test_rejects_a_multiple_of_10_that_is_missing_the_expected_label(self):
        """
        The exact scenario raised: what if a garbage reading (from
        something other than the buy screen) happens to coincidentally be
        a multiple of 10? This is precisely why the label check exists as
        a SECOND, independent defense - "120" alone, with no "MIN NEXT
        ROUND" text anywhere, is correctly rejected despite passing the
        multiple-of-10 check on its own.
        """
        image_bytes = (FIXTURES / "credits_no_label_120.png").read_bytes()
        assert credit_ocr.extract_credits(image_bytes) is None

    def test_returns_none_for_data_that_is_not_a_valid_image_rather_than_crashing(self):
        assert credit_ocr.extract_credits(b"this is not an image") is None

    def test_returns_none_for_empty_bytes(self):
        assert credit_ocr.extract_credits(b"") is None

    def test_raises_a_distinct_error_when_tesseract_itself_cannot_be_found(self, monkeypatch):
        """
        The actual real-world bug this project hit: launchd runs with its
        own minimal PATH, not inheriting the interactive shell's PATH -
        tesseract worked fine typed directly in Terminal but wasn't
        reachable by the backend process. This must surface as a distinct,
        clearly-identifiable error, not silently return None the same way
        as "no digits found in a valid image" does.
        """
        def raise_not_found(*args, **kwargs):
            raise pytest.importorskip("pytesseract").TesseractNotFoundError()

        monkeypatch.setattr(credit_ocr.pytesseract, "image_to_string", raise_not_found)
        image_bytes = (FIXTURES / "credits_4900.png").read_bytes()

        with pytest.raises(credit_ocr.TesseractUnavailableError):
            credit_ocr.extract_credits(image_bytes)


class TestCreditCapValidation:
    """
    Drives extract_credits() through a faked Tesseract output rather than a
    real render, so the validation rule itself is tested directly - no new
    image fixture needed, and no dependence on how a given tesseract build
    happens to read a custom game glyph.
    """

    @staticmethod
    def _ocr_returning(text):
        return lambda *args, **kwargs: text

    def _extract_with_ocr_output(self, monkeypatch, text):
        monkeypatch.setattr(credit_ocr.pytesseract, "image_to_string", self._ocr_returning(text))
        return credit_ocr.extract_credits((FIXTURES / "credits_4900.png").read_bytes())

    def test_the_cap_is_valorant_s_own_9000_credit_maximum(self):
        assert credit_ocr._MAX_CREDITS == 9000

    def test_rejects_the_exact_glyph_misread_shape_a_real_4200_read_as_14200(self, monkeypatch):
        """
        The reported bug: the credit glyph in front of the number is read
        as a leading 1. 14200 passes the label check and passes the
        multiple-of-10 check, so the cap is the only thing standing
        between it and the consensus window.
        """
        assert self._extract_with_ocr_output(monkeypatch, "MIN NEXT ROUND 14200") is None

    def test_accepts_the_cap_itself_rather_than_rejecting_the_boundary(self, monkeypatch):
        assert self._extract_with_ocr_output(monkeypatch, "MIN NEXT ROUND 9000") == 9000

    def test_accepts_the_uncorrupted_reading_the_same_burst_would_also_produce(self, monkeypatch):
        assert self._extract_with_ocr_output(monkeypatch, "MIN NEXT ROUND 4200") == 4200

    def test_the_glyph_is_stripped_as_punctuation_when_tesseract_does_emit_it(self, monkeypatch):
        """
        The cause-side half of the fix: with the glyph whitelisted,
        Tesseract can output it instead of guessing a digit, and the
        digit-stripping regex then removes it - same handling the comma
        already gets.
        """
        assert self._extract_with_ocr_output(monkeypatch, "MIN NEXT ROUND \u00a44200") == 4200

    def test_the_glyph_is_in_the_whitelist_so_tesseract_is_not_forced_to_guess(self):
        assert "\u00a4" in credit_ocr._TESSERACT_CONFIG

    def test_a_value_under_the_cap_inflated_by_the_glyph_is_the_known_uncaught_case(self, monkeypatch):
        """
        Pinned deliberately as a KNOWN LIMIT, not as desired behaviour: a
        real 900 misread as 1900 is under the cap and a multiple of 10, so
        nothing in the text alone can reject it. Documented in finding #4.
        If this ever becomes systematic rather than intermittent, the fix
        is upscaling the crop before OCR - not another validation rule,
        because there is no rule that can tell these apart.
        """
        assert self._extract_with_ocr_output(monkeypatch, "MIN NEXT ROUND 1900") == 1900


class TestContainsExpectedLabel:
    def test_matches_regardless_of_spacing_between_words(self):
        """
        Tesseract's exact word spacing isn't reliably consistent -
        confirmed directly: a real test render came back as
        "MINNEXTROUND" with no spaces at all. The check must match either way.
        """
        assert credit_ocr._contains_expected_label("MIN NEXT ROUND: 4900") is True
        assert credit_ocr._contains_expected_label("MINNEXTROUND 4900") is True

    def test_matches_regardless_of_case(self):
        assert credit_ocr._contains_expected_label("min next round: 4900") is True

    def test_does_not_match_unrelated_text(self):
        assert credit_ocr._contains_expected_label("120") is False
        assert credit_ocr._contains_expected_label("") is False


class TestCorroboratedLatestConsensus:
    """
    Findings #7 and #8. The consensus is the most recent reading, not the
    smallest one in the window - one buy phase now spans however many
    times the menu was opened, so the later look is the one that reflects
    what has actually been bought. A reading that RISES above what the
    window has corroborated waits for a second sighting; one that falls
    does not.
    """

    def test_no_readings_yet_returns_none(self):
        assert credit_ocr.get_predicted_credits() is None

    def test_a_single_reading_becomes_the_consensus(self):
        """
        Nothing can be corroborated one frame into a burst, and the
        alternative to an uncorroborated answer is no answer at all - which
        the roulette reads as "no filter". A single reading is a better
        starting point than that.
        """
        credit_ocr._record_reading(4900)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_a_settled_value_read_repeatedly_is_the_consensus(self):
        for value in [4900, 4900, 4900, 4900]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_the_newest_corroborated_value_wins_over_older_ones(self):
        """
        The whole point of the change. The same real capture log as before:
        4200 held while nothing had been bought, then 3400, then 2400 as
        the last purchase landed. The answer is 2400 because it is the
        newest thing the window agrees on - not because it is the smallest.
        """
        for value in [4200, 4200, 4200, 4200, 3400, 2400, 2400]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 2400

    def test_a_value_that_went_UP_is_followed_too(self):
        """
        The case a minimum could never express, and the reason the rule had
        to change rather than just be re-tuned: weapons can be refunded
        during the buy phase, which raises "min next round". A minimum
        would have reported 2400 here forever.
        """
        for value in [4200, 4200, 2400, 2400, 5100, 5100]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 5100

    def test_a_one_off_HIGH_misread_at_the_end_is_ignored(self):
        """A stray 9999 among consistent 4900s appears once, so it is never corroborated."""
        for value in [4900, 4900, 4900, 9999]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_a_ONE_OFF_LOW_reading_at_the_end_is_taken_anyway(self):
        """
        Finding #8, and a deliberate reversal: this window used to report
        2400 on the theory that a single low reading is a dropped digit.
        The same shape is what a purchase made a fraction of a second
        before Esc looks like, and that is much the commoner cause, so the
        low reading is now taken.

        The trade is stated in the docstring on the other side: if this
        one really was a misread the roster comes out too small for one
        session, where disbelieving it offers weapons that have already
        been paid for.
        """
        for value in [2400, 2400, 2400, 240]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 240

    def test_the_exact_reported_log_a_purchase_on_the_last_frame_before_Esc(self):
        """
        The real capture log that prompted finding #8, verbatim: five
        readings of 6200, then one 3300 as the purchase landed, then
        nothing but blank frames because the menu was already closed.

        Reporting 6200 here is the single worst answer available - it
        tells the roulette the streamer can still afford the gun they have
        just bought.
        """
        for value in [6200, 6200, 6200, 6200, 6200, 3300]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 3300

    def test_a_rise_still_waits_for_a_second_sighting(self):
        """
        The asymmetry stated on its own. Spending is the expected motion
        of "min next round" within a buy phase; a rise is either a refund
        - which you keep shopping after, so the next frame confirms it -
        or finding #4's inflating misread.
        """
        for value in [2400, 2400, 2400, 3300]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 2400
        credit_ocr._record_reading(3300)
        assert credit_ocr.get_predicted_credits() == 3300

    def test_a_late_straggler_from_before_the_purchase_cannot_undo_it(self):
        """
        Two OCR workers means readings can finish out of the order they
        were captured in (finding #5), so the last entry in the window is
        not always the newest capture. Here the pre-purchase 4200 lands
        after the 2400s that superseded it.

        This is exactly why the count is taken over the scan so far rather
        than over the whole window: the OTHER 4200 in this window is older
        than the straggler, and counting the whole window would let it
        vouch for a value the purchase had already replaced.
        """
        for value in [4200, 2400, 2400, 4200]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 2400

    def test_a_value_is_only_corroborated_by_readings_at_least_as_new_as_itself(self):
        """
        The rule stated on its own, minimally. 3400 is in the window twice,
        but the second sighting is older than the 2400s that follow it, so
        it cannot outrank them.
        """
        for value in [3400, 4200, 3400, 2400, 2400]:
            credit_ocr._record_reading(value)
        assert credit_ocr.get_predicted_credits() == 2400

    def test_a_purchase_takes_effect_on_the_very_frame_it_is_first_read(self):
        """
        There is no lag at all on the way down since finding #8 - which is
        the whole point, because the frame a purchase is first read on is
        sometimes the only one there will ever be.
        """
        for value in [4200, 4200, 4200]:
            credit_ocr._record_reading(value)
        credit_ocr._record_reading(2400)
        assert credit_ocr.get_predicted_credits() == 2400

    def test_older_readings_roll_off_once_the_window_is_full(self):
        oldest = [1110, 2220, 3330]
        # Exactly a full window, built from the size rather than written out,
        # so retuning the capture rate doesn't leave this asserting a shape
        # the deque no longer has.
        newest = [4440, 4440, 5300] + [4900] * (credit_ocr._READING_HISTORY_SIZE - 3)
        for value in oldest + newest:
            credit_ocr._record_reading(value)
        # The window is bounded, not an ever-growing history: only the last
        # _READING_HISTORY_SIZE survive.
        assert list(credit_ocr._recent_readings) == newest
        assert 1110 not in credit_ocr._recent_readings
        assert credit_ocr.get_predicted_credits() == 4900

    def test_two_readings_deep_the_corroboration_requirement_is_exactly_two(self):
        assert credit_ocr._CORROBORATING_READINGS == 2

    def test_a_rise_that_is_corroborated_by_a_straggler_behind_it_is_still_refused(self):
        """
        The two rules together on the case that needs both. The pre-purchase
        4200 finished out of order (finding #5) and landed last; the other
        4200 in the window is OLDER than the 2400s, so the scan-so-far count
        never reaches it, and the rise is left uncorroborated and refused.
        """
        for value in [4200, 2400, 2400, 4200]:
            credit_ocr._record_reading(value)
        assert credit_ocr._corroborated_value() == 2400
        assert credit_ocr.get_predicted_credits() == 2400

    def test_the_window_is_about_one_second_at_the_agents_real_capture_rate(self):
        """
        Pinned deliberately rather than left implicit. The size is in
        READINGS, so the count on its own means nothing - what was chosen
        is ~1 second of history, and the count is only how that gets
        expressed at a particular capture rate. The agent captures 20
        images a second (agent.CAPTURE_INTERVAL_WITHIN_BURST, 0.05s, in
        the pc-ocr repo), so 20 readings is that second. Change the rate
        there and this has to move with it - test_agent.py pins the same
        pairing from the other side.
        """
        agent_capture_interval_seconds = 0.05
        assert credit_ocr._recent_readings.maxlen == credit_ocr._READING_HISTORY_SIZE
        seconds_of_history = credit_ocr._READING_HISTORY_SIZE * agent_capture_interval_seconds
        assert round(seconds_of_history, 3) == 1.0


class TestTheWindowExpiresOnItsOwn:
    """
    Finding #7's second half. The agent resets the history at the start of
    a new buy phase, but that POST can simply not arrive - a network blip,
    an agent restarted mid-match, a gaming PC that slept. The window
    therefore also ages out here, so the worst case is no prediction rather
    than a previous round's budget deciding what viewers may vote for.
    """

    def test_a_fresh_window_is_used_normally(self):
        credit_ocr._record_reading(4900)
        credit_ocr._record_reading(4900)
        assert credit_ocr.get_predicted_credits() == 4900
        assert credit_ocr.recent_readings() == [4900, 4900]

    def test_a_window_older_than_the_cutoff_is_treated_as_empty(self, monkeypatch):
        credit_ocr._record_reading(4900)
        credit_ocr._record_reading(4900)
        # Age it past the cutoff without waiting out a real 20 seconds.
        monkeypatch.setattr(
            credit_ocr,
            "_window_last_append_at",
            credit_ocr._window_last_append_at - (credit_ocr._READING_MAX_AGE_SECONDS + 1),
        )
        assert credit_ocr.get_predicted_credits() is None
        assert credit_ocr.recent_readings() == []

    def test_the_panel_and_the_roulette_agree_about_a_stale_window(self, monkeypatch):
        """
        The two are read from the same place for exactly this reason. A
        panel showing a window the roulette will not use is a wrong answer
        about whether the stream is ready, not merely an untidy one.
        """
        credit_ocr._record_reading(3900)
        credit_ocr._record_reading(3900)
        monkeypatch.setattr(
            credit_ocr,
            "_window_last_append_at",
            credit_ocr._window_last_append_at - (credit_ocr._READING_MAX_AGE_SECONDS + 1),
        )
        assert (credit_ocr.get_predicted_credits() is None) == (credit_ocr.recent_readings() == [])

    def test_a_new_reading_revives_the_window(self, monkeypatch):
        credit_ocr._record_reading(4900)
        monkeypatch.setattr(
            credit_ocr,
            "_window_last_append_at",
            credit_ocr._window_last_append_at - (credit_ocr._READING_MAX_AGE_SECONDS + 1),
        )
        assert credit_ocr.get_predicted_credits() is None
        credit_ocr._record_reading(2400)
        # The stale 4900 is still physically in the deque, but the window
        # as a whole is live again and the newest reading is the answer.
        assert credit_ocr.get_predicted_credits() == 2400

    def test_the_backstop_outlasts_a_whole_round(self):
        """
        Finding #9, pinned so the old twenty seconds cannot come back by
        accident. This used to be matched to burst_timer.NEW_ROUND_GAP_SECONDS
        on the gaming PC, which is the gap between two PRESSES of B - a
        completely different duration from the age of a reading. A buy phase
        is read in about two seconds and the round after it runs well over a
        minute, so a twenty-second cutoff threw away a correct reading for
        most of every round.

        The buy-phase header is what ends a round now. This is only the
        backstop under it, and it has to outlast any single round including
        overtime.
        """
        longest_plausible_round_seconds = 100 + 40  # a round plus its buy phase
        assert credit_ocr._READING_MAX_AGE_SECONDS > longest_plausible_round_seconds

    def test_the_backstop_is_overridable_from_config(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_reading_max_age_seconds": 5})
        credit_ocr._record_reading(4900)
        monkeypatch.setattr(credit_ocr, "_window_last_append_at", credit_ocr._window_last_append_at - 6)
        assert credit_ocr.get_predicted_credits() is None

    def test_a_nonsense_config_value_falls_back_rather_than_raising(self, monkeypatch):
        """
        The config editor is a free-text JSON field on a web page. A typo
        here must not take the prediction down with it.
        """
        monkeypatch.setattr(config, "_data", {"ocr_reading_max_age_seconds": "soon"})
        credit_ocr._record_reading(4900)
        assert credit_ocr.get_predicted_credits() == 4900


class TestAnAgentThatWentAway:
    """
    Finding #9's second backstop. The buy-phase header is what ends a
    round, which only works while there is an agent to send it - so an
    agent that has been heard from and then goes quiet has to expire the
    window on its own, or a budget from before the gaming PC crashed would
    stand until the five-minute cap.
    """

    def test_a_window_from_an_agent_that_stopped_reporting_is_dropped(self, monkeypatch):
        ocr_agent.record_heartbeat()
        credit_ocr._record_reading(4900)
        # Age the heartbeat past the cutoff without waiting it out.
        monkeypatch.setattr(
            ocr_agent,
            "_last_heartbeat_at",
            ocr_agent._last_heartbeat_at - (ocr_agent.HEARTBEAT_TIMEOUT_SECONDS + 1),
        )
        assert credit_ocr.get_predicted_credits() is None
        assert credit_ocr.recent_readings() == []

    def test_a_live_agent_keeps_its_window(self):
        ocr_agent.record_heartbeat()
        credit_ocr._record_reading(4900)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_never_having_heard_from_an_agent_is_not_the_same_as_it_going_away(self):
        """
        An agent build older than the heartbeat route sends captures and no
        pings. Reading that as "gone" would discard every reading it ever
        produced, which is the opposite of what this guard is for.
        """
        ocr_agent.reset()
        credit_ocr._record_reading(4900)
        assert credit_ocr._agent_is_gone() is False
        assert credit_ocr.get_predicted_credits() == 4900

    def test_an_untouched_window_is_stale_rather_than_fresh(self):
        """A window nothing has ever been written to must not read as live - _window_last_append_at starts at None precisely so it cannot."""
        assert credit_ocr._window_last_append_at is None
        assert credit_ocr._window_is_stale() is True


async def make_client():
    app = web.Application()
    app.router.add_post("/api/ocr/credit-report", credit_ocr.handle_credit_report)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client


class TestHandleCreditReport:
    @pytest.mark.asyncio
    async def test_accepts_a_real_image_and_reports_both_the_reading_and_consensus(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        client = await make_client()
        image_bytes = (FIXTURES / "credits_4900.png").read_bytes()

        resp = await client.post(
            "/api/ocr/credit-report",
            data=image_bytes,
            headers={"X-Agent-Secret": "test-secret-123"},
        )
        body = await resp.json()

        assert resp.status == 200
        assert body["credits"] == 4900
        assert body["consensus"] == 4900
        assert credit_ocr.get_predicted_credits() == 4900

        await client.close()

    @pytest.mark.asyncio
    async def test_rejects_a_missing_or_wrong_secret(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        client = await make_client()
        resp = await client.post(
            "/api/ocr/credit-report",
            data=(FIXTURES / "credits_4900.png").read_bytes(),
            headers={"X-Agent-Secret": "wrong-secret"},
        )

        assert resp.status == 401
        await client.close()

    @pytest.mark.asyncio
    async def test_rejects_when_no_secret_is_configured_at_all(self, monkeypatch):
        """
        If ocr_agent_secret was never set up, this endpoint must stay
        closed by default - not silently accept unauthenticated requests
        just because nothing was configured yet.
        """
        monkeypatch.setattr(config, "_data", {})

        client = await make_client()
        resp = await client.post(
            "/api/ocr/credit-report",
            data=(FIXTURES / "credits_4900.png").read_bytes(),
            headers={"X-Agent-Secret": ""},
        )

        assert resp.status == 401
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_422_for_an_image_that_fails_validation(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        client = await make_client()
        resp = await client.post(
            "/api/ocr/credit-report",
            data=b"not a valid image at all",
            headers={"X-Agent-Secret": "test-secret-123"},
        )

        assert resp.status == 422
        await client.close()

    @pytest.mark.asyncio
    async def test_returns_503_with_a_distinct_message_when_tesseract_is_unavailable(self, monkeypatch):
        """
        Confirms the actual fix for a real bug: a missing tesseract
        binary now gives a genuinely different, more actionable response
        (503, "tesseract binary not found...") than a normal validation-
        failure case (422) - not the same generic error for both.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        def raise_unavailable(image_bytes):
            raise credit_ocr.TesseractUnavailableError("tesseract binary not found on the Mac Mini")

        monkeypatch.setattr(credit_ocr, "extract_credits", raise_unavailable)

        client = await make_client()
        resp = await client.post(
            "/api/ocr/credit-report",
            data=(FIXTURES / "credits_4900.png").read_bytes(),
            headers={"X-Agent-Secret": "test-secret-123"},
        )
        body = await resp.json()

        assert resp.status == 503
        assert "tesseract binary not found" in body["error"]
        await client.close()

    @pytest.mark.asyncio
    async def test_rejects_an_empty_request_body(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        client = await make_client()
        resp = await client.post(
            "/api/ocr/credit-report",
            data=b"",
            headers={"X-Agent-Secret": "test-secret-123"},
        )

        assert resp.status == 400
        await client.close()

    @pytest.mark.asyncio
    async def test_consensus_updates_correctly_across_multiple_real_requests(self, monkeypatch):
        """
        An end-to-end version of the majority-vote scenario, through the
        real HTTP handler across multiple actual requests, not just
        directly manipulating the reading history.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        client = await make_client()

        # Send 4900 three times - the consensus should stay at 4900
        # throughout, confirmed after each individual request.
        for _ in range(3):
            resp = await client.post(
                "/api/ocr/credit-report",
                data=(FIXTURES / "credits_4900.png").read_bytes(),
                headers={"X-Agent-Secret": "test-secret-123"},
            )
            body = await resp.json()
            assert body["consensus"] == 4900

        await client.close()


class TestTheBuyPhaseHeader:
    """
    Finding #9's real mechanism. A round ends when the next buy phase
    starts, and the gaming PC is the only thing that can see that happen -
    so every capture and every reset carries the id of the phase it
    belongs to, and a change of id is what empties the window.

    That is what lets a reading live for a whole round instead of twenty
    seconds: the age cutoff only existed because a reset POST can go
    missing, and with the id on every capture a missing reset costs
    nothing at all.
    """

    @pytest.mark.asyncio
    async def test_a_new_phase_id_clears_the_window(self):
        credit_ocr._record_reading(4900)
        credit_ocr._record_reading(4900)
        assert await credit_ocr._begin_buy_phase("7", force=False) is True
        assert credit_ocr.recent_readings() == []
        assert credit_ocr.current_buy_phase() == "7"

    @pytest.mark.asyncio
    async def test_the_same_phase_id_arriving_again_changes_nothing(self):
        """
        The reset POST and the first capture of a phase both declare it,
        and they race. Whichever wins does the clearing; the other must
        not clear a second time, because by then this phase's own first
        readings are in the window.
        """
        await credit_ocr._begin_buy_phase("7", force=False)
        credit_ocr._record_reading(3300)
        assert await credit_ocr._begin_buy_phase("7", force=False) is False
        assert credit_ocr.recent_readings() == [3300]

    @pytest.mark.asyncio
    async def test_a_reset_with_no_id_at_all_still_clears(self):
        """An agent build older than the header has nothing to compare, and its POST is the only signal there is."""
        credit_ocr._record_reading(4900)
        assert await credit_ocr._begin_buy_phase(None, force=True) is True
        assert credit_ocr.recent_readings() == []

    @pytest.mark.asyncio
    async def test_listeners_run_once_per_phase_however_it_was_declared(self):
        """
        The forced-buy badge counts buy phases, so a phase announced twice
        would age the badge twice as fast.
        """
        seen = []

        async def listener():
            seen.append(credit_ocr.current_buy_phase())

        credit_ocr.on_new_buy_phase(listener)
        try:
            await credit_ocr._begin_buy_phase("1", force=False)
            await credit_ocr._begin_buy_phase("1", force=False)
            await credit_ocr._begin_buy_phase("2", force=False)
            assert seen == ["1", "2"]
        finally:
            credit_ocr._new_buy_phase_listeners.remove(listener)

    @pytest.mark.asyncio
    async def test_a_reset_that_never_arrives_costs_nothing(self, monkeypatch):
        """
        The failure the old twenty-second cutoff existed to contain, now
        contained by the header instead: round 1's reading is replaced the
        moment a capture from round 2 shows up, with no reset in between.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "s"})
        await credit_ocr._begin_buy_phase("1", force=False)
        credit_ocr._record_reading(6200)
        assert credit_ocr.get_predicted_credits() == 6200

        await credit_ocr._begin_buy_phase("2", force=False)  # a capture from the next round
        assert credit_ocr.get_predicted_credits() is None

    @pytest.mark.asyncio
    async def test_the_reset_route_reads_the_header(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        credit_ocr._record_reading(4900)

        app = web.Application()
        app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        client = TestClient(TestServer(app))
        await client.start_server()

        resp = await client.post(
            "/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123", "X-Buy-Phase": "12"}
        )
        assert resp.status == 200
        assert credit_ocr.current_buy_phase() == "12"
        assert credit_ocr.recent_readings() == []

        # ...and the same phase again is a no-op, so a reset arriving after
        # the phase's first capture cannot wipe it.
        credit_ocr._record_reading(3300)
        resp = await client.post(
            "/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123", "X-Buy-Phase": "12"}
        )
        assert resp.status == 200
        assert credit_ocr.recent_readings() == [3300]

        await client.close()

    @pytest.mark.asyncio
    async def test_a_second_look_at_the_same_buy_phase_keeps_its_readings(self, monkeypatch):
        """
        Fix #10 from the other side. Re-opening the menu inside one buy
        phase does not bump the id on the gaming PC, so nothing here clears
        - which is the whole reason the id is bumped by the round gap
        rather than by the keypress.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        await credit_ocr._begin_buy_phase("4", force=False)
        credit_ocr._record_reading(6200)

        app = web.Application()
        app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        client = TestClient(TestServer(app))
        await client.start_server()
        resp = await client.post(
            "/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123", "X-Buy-Phase": "4"}
        )
        assert resp.status == 200
        assert credit_ocr.recent_readings() == [6200]
        await client.close()


class TestHandleReset:
    @pytest.mark.asyncio
    async def test_clears_the_reading_history_with_the_correct_secret(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        for value in [4900, 4900, 5300]:
            credit_ocr._record_reading(value)

        app = web.Application()
        app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        resp = await client.post("/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123"})

        assert resp.status == 200
        assert len(credit_ocr._recent_readings) == 0
        assert credit_ocr.get_predicted_credits() is None

        await client.close()

    @pytest.mark.asyncio
    async def test_rejects_a_wrong_secret_without_clearing_anything(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        for value in [4900, 4900]:
            credit_ocr._record_reading(value)

        app = web.Application()
        app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()

        resp = await client.post("/api/ocr/reset", headers={"X-Agent-Secret": "wrong-secret"})

        assert resp.status == 401
        assert len(credit_ocr._recent_readings) == 2  # untouched

        await client.close()

    @pytest.mark.asyncio
    async def test_the_actual_real_bug_this_fixes_cross_round_contamination(self, monkeypatch):
        """
        The exact scenario reported: without a reset between rounds, the
        first readings of a NEW round's buy phase get voted alongside
        stale leftovers from the PREVIOUS round, skewing the new round's
        consensus. Confirms the reset genuinely breaks that contamination.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})

        report_app = web.Application()
        report_app.router.add_post("/api/ocr/credit-report", credit_ocr.handle_credit_report)
        report_app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        server = TestServer(report_app)
        client = TestClient(server)
        await client.start_server()

        # Round 1 finishes at 10000 (a strong majority)
        for _ in range(6):
            await client.post(
                "/api/ocr/credit-report",
                data=(FIXTURES / "credits_4900.png").read_bytes(),  # reusing fixture bytes, value itself doesn't matter here
                headers={"X-Agent-Secret": "test-secret-123"},
            )
        assert credit_ocr.get_predicted_credits() == 4900

        # WITHOUT a reset, round 2's first reading would be outvoted by
        # round 1's 6 leftover entries still sitting in the window.
        # With the reset (what the agent now correctly calls), the slate
        # is genuinely clean for the new round.
        await client.post("/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123"})
        assert credit_ocr.get_predicted_credits() is None  # confirms truly empty, not just outvoted

        await client.close()


class TestLastReadingSurvivesAReset:
    """
    Finding #6. The rolling window is cleared at the start of every buy
    phase, which is right for the consensus and useless for answering "does
    any of this work". Between rounds - most of a match - an empty window
    and a pipeline that has never once succeeded look identical, and the
    dashboard rendered both as "No reading yet" while the agent was
    printing real numbers on the other machine.
    """

    def test_nothing_read_yet_reports_no_credits_and_no_age(self):
        assert credit_ocr.last_reading() == {"credits": None, "age_seconds": None}

    def test_an_accepted_reading_is_remembered_with_an_age(self):
        credit_ocr._remember_last_reading(3900)
        remembered = credit_ocr.last_reading()
        assert remembered["credits"] == 3900
        assert remembered["age_seconds"] is not None
        assert remembered["age_seconds"] >= 0

    def test_the_newest_accepted_reading_replaces_the_previous_one(self):
        credit_ocr._remember_last_reading(4200)
        credit_ocr._remember_last_reading(2400)
        assert credit_ocr.last_reading()["credits"] == 2400

    @pytest.mark.asyncio
    async def test_a_reset_empties_the_window_but_keeps_the_last_reading(self, monkeypatch):
        """The whole point: these two histories are cleared by different things."""
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        for value in [4200, 3400, 2400]:
            credit_ocr._record_reading(value)
        credit_ocr._remember_last_reading(2400)

        app = web.Application()
        app.router.add_post("/api/ocr/reset", credit_ocr.handle_reset)
        server = TestServer(app)
        client = TestClient(server)
        await client.start_server()
        await client.post("/api/ocr/reset", headers={"X-Agent-Secret": "test-secret-123"})
        await client.close()

        assert credit_ocr.get_predicted_credits() is None
        assert credit_ocr.last_reading()["credits"] == 2400

    def test_the_prediction_never_falls_back_to_it(self):
        """
        A reading from a previous round is not a budget. get_predicted_credits()
        must stay None so the roulette fails open to the full roster rather
        than filtering on a number that is no longer true.
        """
        credit_ocr._remember_last_reading(2400)
        assert credit_ocr.get_predicted_credits() is None

    @pytest.mark.asyncio
    async def test_a_real_accepted_capture_populates_it_end_to_end(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        client = await make_client()
        await client.post(
            "/api/ocr/credit-report",
            data=(FIXTURES / "credits_4900.png").read_bytes(),
            headers={"X-Agent-Secret": "test-secret-123"},
        )
        await client.close()
        assert credit_ocr.last_reading()["credits"] == 4900
