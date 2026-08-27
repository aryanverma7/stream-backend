import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import ocr_agent


@pytest.fixture(autouse=True)
def clean_agent_state():
    ocr_agent.reset()
    yield
    ocr_agent.reset()


class TestConnectedFlag:
    def test_a_fresh_backend_has_never_seen_the_agent(self):
        status = ocr_agent.status()
        assert status["connected"] is False
        assert status["last_heartbeat_age_seconds"] is None
        assert status["last_capture_age_seconds"] is None

    def test_a_heartbeat_makes_it_connected(self):
        ocr_agent.record_heartbeat()
        status = ocr_agent.status()
        assert status["connected"] is True
        assert status["last_heartbeat_age_seconds"] < 1

    def test_a_stale_heartbeat_does_not(self):
        ocr_agent._last_heartbeat_at = time.time() - (ocr_agent.HEARTBEAT_TIMEOUT_SECONDS + 1)
        assert ocr_agent.status()["connected"] is False

    def test_a_heartbeat_right_on_the_timeout_still_counts(self):
        # The boundary is inclusive on purpose - the timeout is three
        # missed pings, and the third one landing exactly on it is not a
        # miss.
        ocr_agent._last_heartbeat_at = time.time() - ocr_agent.HEARTBEAT_TIMEOUT_SECONDS
        assert ocr_agent.status()["connected"] is True

    def test_a_recent_capture_counts_as_proof_of_life_on_its_own(self):
        # An agent build older than the heartbeat sends no pings at all.
        # While it is actually sending work it must not be reported dead.
        ocr_agent.record_capture(accepted=True)
        assert ocr_agent.status()["connected"] is True
        assert ocr_agent.status()["last_heartbeat_age_seconds"] is None

    def test_a_stale_capture_alongside_a_fresh_heartbeat_stays_connected(self):
        # The normal state between rounds: pings arriving, no captures.
        ocr_agent._last_capture_at = time.time() - 600
        ocr_agent.record_heartbeat()
        assert ocr_agent.status()["connected"] is True


class TestCaptureCounters:
    def test_a_rejected_capture_still_counts_as_received(self):
        # 422s are the expected outcome for most frames of a burst, and
        # they still prove the agent and the network path are working.
        ocr_agent.record_capture(accepted=False)
        status = ocr_agent.status()
        assert status["captures_received"] == 1
        assert status["captures_accepted"] == 0
        assert status["last_accepted_age_seconds"] is None

    def test_an_accepted_capture_counts_in_both(self):
        ocr_agent.record_capture(accepted=True)
        status = ocr_agent.status()
        assert status["captures_received"] == 1
        assert status["captures_accepted"] == 1
        assert status["last_accepted_age_seconds"] is not None

    def test_counters_accumulate_across_a_burst(self):
        for accepted in (False, False, True, False, True):
            ocr_agent.record_capture(accepted=accepted)
        status = ocr_agent.status()
        assert status["captures_received"] == 5
        assert status["captures_accepted"] == 2

    def test_the_timeout_is_reported_so_the_panel_can_explain_itself(self):
        assert ocr_agent.status()["heartbeat_timeout_seconds"] == ocr_agent.HEARTBEAT_TIMEOUT_SECONDS
