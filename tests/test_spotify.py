import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import spotify
from config import config

CONNECTED = {
    "spotify_refresh_token": "refresh-abc",
    "spotify_client_id": "id",
    "spotify_client_secret": "secret",
    "spotify_request_cost": 100,
}

TRACK = {
    "uri": "spotify:track:4cOdK2wGLETKBW3PvgPWqT",
    "name": "Never Gonna Give You Up",
    "artists": [{"name": "Rick Astley"}],
    "duration_ms": 213_000,
}


@pytest.fixture(autouse=True)
def clean_token():
    spotify.forget_token()
    yield
    spotify.forget_token()


class TestTrackReferences:
    """
    People paste links. Running a URL through the search endpoint finds
    nothing, so a pasted reference has to be recognised as one.
    """

    def test_reads_an_open_spotify_url(self):
        assert (
            spotify.track_id_from("https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT?si=abc")
            == "4cOdK2wGLETKBW3PvgPWqT"
        )

    def test_reads_a_localised_url(self):
        """Spotify's share links carry a locale segment for most of the world."""
        assert (
            spotify.track_id_from("https://open.spotify.com/intl-de/track/4cOdK2wGLETKBW3PvgPWqT")
            == "4cOdK2wGLETKBW3PvgPWqT"
        )

    def test_reads_a_uri(self):
        assert spotify.track_id_from("spotify:track:4cOdK2wGLETKBW3PvgPWqT") == "4cOdK2wGLETKBW3PvgPWqT"

    def test_plain_text_is_not_a_reference(self):
        assert spotify.track_id_from("never gonna give you up") is None

    def test_an_album_link_is_not_a_track(self):
        assert spotify.track_id_from("https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT") is None


class TestDescribe:
    def test_artist_and_title(self):
        assert spotify.describe(TRACK) == "Rick Astley - Never Gonna Give You Up"

    def test_multiple_artists(self):
        track = {"name": "Song", "artists": [{"name": "A"}, {"name": "B"}]}
        assert spotify.describe(track) == "A, B - Song"

    def test_a_track_with_no_artist_still_renders(self):
        assert spotify.describe({"name": "Untitled"}) == "Untitled"


class TestRequestSong:
    @pytest.mark.asyncio
    async def test_queues_and_charges(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        spend = AsyncMock(return_value=(True, None))
        queue = AsyncMock()
        monkeypatch.setattr(spotify, "try_spend", spend)
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "add_to_queue", queue)

        result = await spotify.request_song("someviewer", "never gonna give you up")

        assert result["ok"] is True
        assert result["description"] == "Rick Astley - Never Gonna Give You Up"
        spend.assert_awaited_once()
        queue.assert_awaited_once_with(TRACK["uri"])

    @pytest.mark.asyncio
    async def test_a_pasted_link_skips_the_search(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        search = AsyncMock()
        monkeypatch.setattr(spotify, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(spotify, "search_track", search)
        monkeypatch.setattr(spotify, "get_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "add_to_queue", AsyncMock())

        result = await spotify.request_song("someviewer", "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT")

        assert result["ok"] is True
        search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_song_that_does_not_exist_costs_nothing(self, monkeypatch):
        """
        Resolved before charging on purpose. Charging and refunding for a
        typo leaves two entries in the viewer's history for nothing, and
        makes their mistake look like a fault in the system.
        """
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(spotify, "try_spend", spend)
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=None))

        result = await spotify.request_song("someviewer", "asdkjhaskdjh")

        assert result["ok"] is False
        assert "Couldn't find" in result["reason"]
        spend.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_viewer_who_cannot_afford_it_is_told_what_it_costs(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "try_spend", AsyncMock(return_value=(False, 40)))
        queue = AsyncMock()
        monkeypatch.setattr(spotify, "add_to_queue", queue)

        result = await spotify.request_song("someviewer", "a song")

        assert result["ok"] is False
        assert "100 points" in result["reason"]
        assert "you have 40" in result["reason"]
        queue.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_track_longer_than_the_limit_is_refused_before_charging(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {**CONNECTED, "spotify_max_track_seconds": 300})
        spend = AsyncMock(return_value=(True, None))
        monkeypatch.setattr(spotify, "try_spend", spend)
        monkeypatch.setattr(
            spotify, "search_track", AsyncMock(return_value={**TRACK, "duration_ms": 1_140_000})
        )

        result = await spotify.request_song("someviewer", "a very long song")

        assert result["ok"] is False
        assert "19 minutes" in result["reason"]
        spend.assert_not_awaited()


class TestTheRefundPath:
    """
    Charged, then the queue call fails. The one outcome worth writing
    extra code to avoid is a viewer paying for a song that never plays.
    """

    @pytest.mark.asyncio
    async def test_a_failed_queue_refunds(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        grant = AsyncMock(return_value=None)
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(spotify, "grant_points", grant)
        monkeypatch.setattr(
            spotify,
            "add_to_queue",
            AsyncMock(side_effect=spotify.SpotifyUnavailable("Spotify isn't playing anything right now")),
        )

        result = await spotify.request_song("someviewer", "a song", platform="youtube")

        assert result["ok"] is False
        assert "your points are back" in result["reason"]
        assert "isn't playing anything" in result["reason"]
        grant.assert_awaited_once_with("someviewer", 100, platform="youtube")

    @pytest.mark.asyncio
    async def test_a_failed_refund_is_reported_not_swallowed(self, monkeypatch):
        """
        Nothing here can fix it, so the only useful behaviour is to say so
        in the log loudly enough that a human gives the points back.
        """
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "try_spend", AsyncMock(return_value=(True, None)))
        monkeypatch.setattr(spotify, "add_to_queue", AsyncMock(side_effect=RuntimeError("boom")))
        monkeypatch.setattr(spotify, "grant_points", AsyncMock(side_effect=RuntimeError("cloudbot down")))

        result = await spotify.request_song("someviewer", "a song")

        # The viewer still gets an answer rather than an exception.
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_a_free_request_has_nothing_to_refund(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {**CONNECTED, "spotify_request_cost": 0})
        grant = AsyncMock()
        monkeypatch.setattr(spotify, "search_track", AsyncMock(return_value=TRACK))
        monkeypatch.setattr(spotify, "grant_points", grant)
        monkeypatch.setattr(spotify, "add_to_queue", AsyncMock(side_effect=RuntimeError("boom")))

        result = await spotify.request_song("someviewer", "a song")

        assert result["ok"] is False
        grant.assert_not_awaited()


class TestSwitchedOffOrNotSetUp:
    @pytest.mark.asyncio
    async def test_says_so_when_not_connected(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        result = await spotify.request_song("someviewer", "a song")
        assert result["ok"] is False
        assert "aren't set up" in result["reason"]

    @pytest.mark.asyncio
    async def test_can_be_switched_off_without_disconnecting(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {**CONNECTED, "spotify_song_requests_enabled": False})
        result = await spotify.request_song("someviewer", "a song")
        assert result["ok"] is False
        assert "switched off" in result["reason"]

    @pytest.mark.asyncio
    async def test_an_empty_request_explains_the_command(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        result = await spotify.request_song("someviewer", "   ")
        assert result["ok"] is False
        assert "!sr" in result["reason"]
        assert "100 points" in result["reason"]


class TestTokenCaching:
    @pytest.mark.asyncio
    async def test_a_cached_token_is_reused(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        monkeypatch.setattr(spotify, "_access_token", "cached-token")
        monkeypatch.setattr(spotify, "_access_token_expires_at", time.time() + 600)

        # No session factory is patched, so reaching the network would
        # fail the test rather than quietly succeed.
        assert await spotify.access_token() == "cached-token"

    @pytest.mark.asyncio
    async def test_no_refresh_token_is_its_own_error(self, monkeypatch):
        """
        Distinct from Spotify being down: nobody has ever connected, and
        the fix is a visit to /auth/spotify/login rather than a retry.
        """
        monkeypatch.setattr(config, "_data", {})
        with pytest.raises(spotify.SpotifyNotConfigured):
            await spotify.access_token()

    @pytest.mark.asyncio
    async def test_an_expired_token_is_not_reused(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        monkeypatch.setattr(spotify, "_access_token", "stale")
        monkeypatch.setattr(spotify, "_access_token_expires_at", time.time() - 1)

        # Falls through to the refresh, which has no credentials here.
        with pytest.raises(spotify.SpotifyNotConfigured):
            await spotify.access_token()


class TestStatus:
    def test_reports_not_configured_before_the_oauth_flow(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        assert spotify.status()["configured"] is False

    def test_reports_configured_once_a_refresh_token_exists(self, monkeypatch):
        monkeypatch.setattr(config, "_data", dict(CONNECTED))
        status = spotify.status()
        assert status["configured"] is True
        assert status["request_cost"] == 100
