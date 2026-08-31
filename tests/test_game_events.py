import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import game_events
import roulette
from config import config

SECRET = {"ocr_agent_secret": "test-secret-123"}


@pytest.fixture(autouse=True)
def reset_game_state():
    """
    Module-level state by design - this tracks one live game, the way the
    real thing does - so it has to be cleared between tests.
    """
    game_events.reset()
    yield
    game_events.reset()


async def make_client():
    app = web.Application()
    game_events.register_routes(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def snapshot(**fields):
    return {"app_version": "1.0.0", "game_running": True, "state": fields}


async def post(client, **fields):
    return await client.post(
        "/api/game/state",
        headers={"X-Agent-Secret": "test-secret-123"},
        json=snapshot(**fields),
    )


class TestAuth:
    @pytest.mark.asyncio
    async def test_rejects_a_request_with_no_secret(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        resp = await client.post("/api/game/state", json=snapshot(round_phase="shopping"))
        assert resp.status == 401
        assert game_events.round_phase() is None
        await client.close()

    @pytest.mark.asyncio
    async def test_rejects_a_wrong_secret(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        resp = await client.post(
            "/api/game/state", headers={"X-Agent-Secret": "nope"}, json=snapshot(round_phase="shopping")
        )
        assert resp.status == 401
        await client.close()

    @pytest.mark.asyncio
    async def test_an_unset_secret_does_not_accept_everything(self, monkeypatch):
        """
        A fresh install has no secret configured, and the failure mode to
        avoid is a blank expected value matching a blank provided one -
        this route is in auth.py's open_paths, so its own check is the only
        thing in front of it.
        """
        monkeypatch.setattr(config, "_data", {"ocr_agent_secret": ""})
        client = await make_client()
        resp = await client.post("/api/game/state", headers={"X-Agent-Secret": ""}, json=snapshot())
        assert resp.status == 401
        await client.close()


class TestSnapshotMerging:
    @pytest.mark.asyncio
    async def test_applies_the_fields_it_was_sent(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        resp = await post(client, round_phase="combat", round_number=4, money=3200)
        body = await resp.json()

        assert resp.status == 200
        assert sorted(body["applied"]) == ["money", "round_number", "round_phase"]
        assert game_events.round_phase() == "combat"
        assert game_events.local_money() == 3200
        await client.close()

    @pytest.mark.asyncio
    async def test_a_field_that_did_not_change_is_not_reported_as_applied(self, monkeypatch):
        """
        `applied` is the only way the gaming PC can tell "the backend has
        this" apart from "the backend took my POST and ignored all of it
        because the field names are wrong."
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        await post(client, round_phase="combat")
        resp = await post(client, round_phase="combat")
        assert (await resp.json())["applied"] == []
        await client.close()

    @pytest.mark.asyncio
    async def test_a_field_not_sent_is_left_alone(self, monkeypatch):
        """
        Absent is not the same as null. The app sends every tracked field
        every time, but a partial POST from an older build must not erase
        what it does not mention.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        await post(client, money=3200)
        await post(client, round_phase="combat")
        assert game_events.local_money() == 3200
        await client.close()

    @pytest.mark.asyncio
    async def test_a_body_that_is_not_json_is_refused_not_crashed(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        resp = await client.post(
            "/api/game/state", headers={"X-Agent-Secret": "test-secret-123"}, data=b"not json"
        )
        assert resp.status == 400
        await client.close()


class TestTheBuyPhaseSignal:
    """
    The reason this module is wired to the roulette at all. Every existing
    way of knowing a buy phase started is an inference from a keystroke;
    this is the game saying so.
    """

    @pytest.mark.asyncio
    async def test_fires_when_the_phase_becomes_shopping(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        fired = []
        game_events.on_buy_phase(lambda: _record(fired))
        client = await make_client()

        await post(client, round_phase="combat")
        assert fired == []
        await post(client, round_phase="shopping")
        assert len(fired) == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_does_not_fire_again_while_the_phase_is_unchanged(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        fired = []
        game_events.on_buy_phase(lambda: _record(fired))
        client = await make_client()

        await post(client, round_phase="shopping")
        await post(client, round_phase="shopping", money=4000)
        await post(client, round_phase="shopping", money=2900)
        assert len(fired) == 1
        await client.close()

    @pytest.mark.asyncio
    async def test_fires_once_per_round(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        fired = []
        game_events.on_buy_phase(lambda: _record(fired))
        client = await make_client()

        for phase in ("shopping", "combat", "end", "shopping", "combat"):
            await post(client, round_phase=phase)
        assert len(fired) == 2
        await client.close()

    @pytest.mark.asyncio
    async def test_a_raising_listener_does_not_fail_the_request(self, monkeypatch):
        """
        From the gaming PC a 500 is indistinguishable from the snapshot not
        landing, and there is nothing useful to retry.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))

        async def boom():
            raise RuntimeError("listener exploded")

        game_events.on_buy_phase(boom)
        client = await make_client()
        resp = await post(client, round_phase="shopping")
        assert resp.status == 200
        assert game_events.round_phase() == "shopping"
        await client.close()


class TestRoundResults:
    """
    GEP has no per-round win event - `score` is just the running total - so
    the result is the difference between two snapshots.
    """

    @pytest.mark.asyncio
    async def test_a_rising_won_count_is_a_round_won(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        results = []
        game_events.on_round_result(lambda won: _record(results, won))
        client = await make_client()

        await post(client, score={"won": 3, "lost": 1})
        await post(client, score={"won": 4, "lost": 1})
        assert results == [True]
        await client.close()

    @pytest.mark.asyncio
    async def test_a_rising_lost_count_is_a_round_lost(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        results = []
        game_events.on_round_result(lambda won: _record(results, won))
        client = await make_client()

        await post(client, score={"won": 3, "lost": 1})
        await post(client, score={"won": 3, "lost": 2})
        assert results == [False]
        await client.close()

    @pytest.mark.asyncio
    async def test_a_score_going_down_is_a_new_match_not_thirteen_losses(self, monkeypatch):
        """
        Both numbers reset to zero between games. Reading that as a run of
        losses would be a spectacular way to settle a bet.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        results = []
        game_events.on_round_result(lambda won: _record(results, won))
        client = await make_client()

        await post(client, score={"won": 13, "lost": 7})
        await post(client, score={"won": 0, "lost": 0})
        assert results == []
        await client.close()

    @pytest.mark.asyncio
    async def test_the_first_score_ever_seen_settles_nothing(self, monkeypatch):
        """
        Joining a match already in progress reports 6:4 out of nowhere.
        That is a state, not ten round results.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        results = []
        game_events.on_round_result(lambda won: _record(results, won))
        client = await make_client()

        await post(client, score={"won": 6, "lost": 4})
        assert results == []
        await client.close()


class TestMatchOutcome:
    @pytest.mark.asyncio
    async def test_reports_a_real_outcome(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        outcomes = []
        game_events.on_match_result(lambda outcome: _record(outcomes, outcome))
        client = await make_client()

        await post(client, match_outcome="victory")
        assert outcomes == ["victory"]
        await client.close()

    @pytest.mark.asyncio
    async def test_clearing_the_outcome_between_matches_is_not_a_result(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        outcomes = []
        game_events.on_match_result(lambda outcome: _record(outcomes, outcome))
        client = await make_client()

        await post(client, match_outcome="defeat")
        await post(client, match_outcome=None)
        assert outcomes == ["defeat"]
        await client.close()


class TestTheAgentIsSetAutomatically:
    """
    The !agent command exists because nothing here could see which agent
    was being played. GEP can, so nobody has to type it.
    """

    @pytest.mark.asyncio
    async def test_sets_the_roulette_agent_from_the_game(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        calls = []
        monkeypatch.setattr(roulette, "set_agent", lambda name: calls.append(name) or (name, 600))
        client = await make_client()

        await post(client, agent="Cypher")
        assert calls == ["cypher"]
        await client.close()

    @pytest.mark.asyncio
    async def test_does_not_rewrite_config_on_every_snapshot(self, monkeypatch):
        """
        set_agent writes config.json and saves it. Once per match is fine;
        once per snapshot is a disk write every fifteen seconds forever.
        """
        monkeypatch.setattr(config, "_data", {**SECRET, "roulette_current_agent": "cypher"})
        calls = []
        monkeypatch.setattr(roulette, "set_agent", lambda name: calls.append(name) or (name, 600))
        client = await make_client()

        await post(client, agent="cypher", money=4000)
        await post(client, agent="cypher", money=2900)
        assert calls == []
        await client.close()


class TestLiveness:
    @pytest.mark.asyncio
    async def test_a_backend_that_has_heard_nothing_is_not_connected(self):
        assert game_events.is_connected() is False
        assert game_events.round_phase() is None
        assert game_events.local_money() is None

    @pytest.mark.asyncio
    async def test_a_stale_snapshot_stops_being_believed(self, monkeypatch):
        """
        Same rule as everywhere else here: a stale answer is worse than no
        answer, because something downstream will act on it.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        await post(client, round_phase="shopping", money=4200)
        assert game_events.local_money() == 4200

        monkeypatch.setattr(
            game_events,
            "_last_snapshot_at",
            game_events._last_snapshot_at - (game_events.SNAPSHOT_TIMEOUT_SECONDS + 1),
        )
        assert game_events.is_connected() is False
        assert game_events.local_money() is None
        assert game_events.round_phase() is None
        await client.close()

    def test_the_timeout_fits_three_of_the_apps_heartbeats(self):
        """
        The pairing that spans both machines: app_config.js sends a
        snapshot every 15 seconds, so two may be dropped before this
        backend is allowed to call the gaming PC dead. Same ratio
        ocr_agent.HEARTBEAT_TIMEOUT_SECONDS keeps against the OCR agent.
        """
        app_heartbeat_seconds = 15  # app_config.js heartbeatSeconds
        assert app_heartbeat_seconds * 3 <= game_events.SNAPSHOT_TIMEOUT_SECONDS

    @pytest.mark.asyncio
    async def test_the_game_closing_clears_the_match(self, monkeypatch):
        """
        Otherwise the dashboard shows a buy phase that ended when the
        streamer quit to desktop ten minutes ago.
        """
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        await post(client, round_phase="shopping", score={"won": 5, "lost": 2}, money=4200)

        await client.post(
            "/api/game/state",
            headers={"X-Agent-Secret": "test-secret-123"},
            json={"game_running": False, "state": {}},
        )
        assert game_events.round_phase() is None
        assert game_events.local_money() is None
        assert game_events.status()["score"] is None
        await client.close()


class TestStatusForTheDashboard:
    @pytest.mark.asyncio
    async def test_reports_the_live_game(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(SECRET))
        client = await make_client()
        await post(
            client,
            round_phase="shopping",
            round_number=7,
            score={"won": 4, "lost": 2},
            money=4200,
            agent="cypher",
            map="ascent",
            game_mode="competitive",
        )

        status = game_events.status()
        assert status["connected"] is True
        assert status["round_phase"] == "shopping"
        assert status["score"] == {"won": 4, "lost": 2}
        assert status["money"] == 4200
        assert status["last_snapshot_age_seconds"] >= 0
        await client.close()


def _record(bucket, *args):
    """
    Turns a plain lambda into the coroutine the listener lists expect, so
    the tests above read as one line rather than a nested async def each.
    """
    async def run():
        bucket.append(args[0] if len(args) == 1 else True)
    return run()
