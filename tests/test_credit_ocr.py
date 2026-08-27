import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import credit_ocr
from config import config

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def reset_reading_history():
    """Every test gets a fresh reading history - this module uses shared,
    module-level state by design (a real rolling window across real
    requests), which means tests must reset it between runs."""
    credit_ocr._recent_readings.clear()
    credit_ocr.forget_last_reading()
    yield
    credit_ocr._recent_readings.clear()
    credit_ocr.forget_last_reading()


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


class TestMinimumValueConsensus:
    def test_no_readings_yet_returns_none(self):
        assert credit_ocr.get_predicted_credits() is None

    def test_a_single_reading_becomes_the_consensus(self):
        credit_ocr._recent_readings.append(4900)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_the_exact_scenario_described_10000_5300_4900_4900_picks_4900(self):
        """The exact real example given: the smallest value in the recent
        history wins, whether or not it's the most recent one - here it
        happens to be both."""
        for value in [10000, 5300, 4900, 4900]:
            credit_ocr._recent_readings.append(value)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_older_readings_roll_off_once_the_window_is_full(self):
        oldest = [1110, 2220, 3330]
        newest = [4440, 10000, 5300, 4900, 4900, 4900, 4900, 4900, 4900, 4900]  # exactly a full window
        for value in oldest + newest:
            credit_ocr._recent_readings.append(value)
        # The window is bounded, not an ever-growing history: only the last
        # _READING_HISTORY_SIZE survive, so 1110 - which would otherwise win
        # the minimum outright - has rolled off entirely.
        assert list(credit_ocr._recent_readings) == newest
        assert 1110 not in credit_ocr._recent_readings
        assert credit_ocr.get_predicted_credits() == 4440

    def test_the_window_is_about_one_second_at_the_agents_real_capture_rate(self):
        """
        Pinned deliberately rather than left implicit. The size is in
        READINGS, so the count on its own means nothing - what was chosen
        is ~1 second of history, and the count is only how that gets
        expressed at a particular capture rate. The agent captures 10
        images a second (agent.CAPTURE_INTERVAL_WITHIN_BURST, 0.1s, in the
        pc-ocr repo), so 10 readings is that second. Change the rate there
        and this has to move with it - test_agent.py pins the same pairing
        from the other side.
        """
        agent_capture_interval_seconds = 0.1
        assert credit_ocr._recent_readings.maxlen == credit_ocr._READING_HISTORY_SIZE
        seconds_of_history = credit_ocr._READING_HISTORY_SIZE * agent_capture_interval_seconds
        assert round(seconds_of_history, 3) == 1.0

    def test_one_upward_misread_does_not_override_three_consistent_ones(self):
        """Credits only ever decrease within a single burst, so a reading
        HIGHER than everything else (like a stray "9999" here) can never
        be the true minimum - the minimum-based consensus rejects it for
        the same reason majority vote used to, just via a different rule."""
        for value in [4900, 4900, 4900, 9999]:
            credit_ocr._recent_readings.append(value)
        assert credit_ocr.get_predicted_credits() == 4900

    def test_the_real_bug_this_replaced_a_late_lower_reading_beats_an_earlier_majority(self):
        """The exact real capture log that motivated switching away from
        majority vote: four readings held at 4200 (before any purchase),
        then 3400, then 2400 right before the menu closed. Majority vote
        picked the stale, already-spent 4200 (four votes to one); the
        true final "min next round" was 2400."""
        for value in [4200, 4200, 4200, 4200, 3400, 2400]:
            credit_ocr._recent_readings.append(value)
        assert credit_ocr.get_predicted_credits() == 2400


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


class TestHandleReset:
    @pytest.mark.asyncio
    async def test_clears_the_reading_history_with_the_correct_secret(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": "test-secret-123"})
        credit_ocr._recent_readings.extend([4900, 4900, 5300])

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
        credit_ocr._recent_readings.extend([4900, 4900])

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
        credit_ocr._recent_readings.extend([4200, 3400, 2400])
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
