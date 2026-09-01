"""
Spotify song requests (Task #12).

Viewers spend points to put a track in the queue on the streamer's own
Spotify: `!song <name or link>` searches, or reads a pasted Spotify link,
and queues it. There is deliberately no chat command for "what is playing"
- that has its own overlay on stream, and a command answering a question
the viewer can already see would be a command spent on nothing.
`!songqueue` is the exception that proves it: what is COMING is not on the
overlay, and it is the question someone asks right after paying.

Nightbot cannot do this - its song requests are YouTube and SoundCloud
only, and its own position is that streaming full Spotify tracks would
breach Spotify's terms - so every "Nightbot + Spotify" tool is a separate
thing talking to Spotify itself. This is that separate thing, for this
stack.

**Three hard requirements, all of them Spotify's and none negotiable:**

  1. Spotify **Premium**. `POST /me/player/queue` returns 403 without it.
  2. An **active device**. The queue endpoint targets whatever the account
     is currently playing on, so Spotify has to actually be playing
     somewhere - open-but-idle is not enough, and produces a 404 with
     reason NO_ACTIVE_DEVICE. This is the failure that will happen most
     often in practice, so it gets its own message rather than a generic
     one: "start playing something first" is actionable, "request failed"
     is not.
  3. The `user-modify-playback-state` scope, plus the two read scopes -
     those are for the now-playing overlay, not for chat.

**Development Mode is enough and Extended Access is not needed**, which is
worth writing down because it is the opposite of the Streamlabs Loyalty
situation this project spent months on. From 9 March 2026 Spotify caps a
dev-mode app at five users and requires the app owner to hold Premium -
and this app controls exactly one account, the streamer's own, so the cap
is irrelevant. Extended Access is now reserved for "established, scalable"
products and would be a hard refusal for a personal stream tool, so
needing it would have killed the feature outright. The endpoints that were
withdrawn from dev mode are the batch and browse ones; the Player API is
not among them.

**Tokens.** The refresh token is long-lived and lives in config.json; the
access token lasts an hour and is kept in memory only. It is refreshed on
demand rather than on a timer - there is no background job to get wrong,
and a backend that has been idle overnight refreshes on the first request
of the day instead of having spent the night refreshing a token nobody
wanted. Refreshing needs the client secret, so it happens here and never
anywhere a widget could reach.

**The now-playing overlay is fed from here**, not by the widget talking to
Spotify itself - which is what keeps the client secret in this process,
and is also what lets the overlay say who requested the current track.
That last part is the thing an off-the-shelf Spotify widget cannot do: it
has no idea a request system exists. See `_recent_requests` and
`start_now_playing_poller`.

**Points are taken before the queue call and refunded if it fails**, the
same shape roulette.trigger_roulette uses and for the same reason: a
viewer charged for a song that never played is the one outcome worth
writing extra code to avoid. The refund is best-effort and says so loudly
in the log if it cannot be made, because that is the case a human has to
fix by hand.
"""
import asyncio
import base64
import re
import time

import aiohttp

from config import config
from logger import get_logger
from points import UnknownUser, grant_points, try_spend

log = get_logger("Spotify")

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Queueing needs modify; the now-playing overlay needs the two reads.
# Requested together at consent time because Spotify only asks once -
# adding a scope later means sending the streamer back through the whole
# flow, and (see spotify_oauth) doing so quietly returns the OLD scopes
# unless the consent screen is forced.
SCOPES = "user-modify-playback-state user-read-playback-state user-read-currently-playing"

DEFAULT_REQUEST_COST = 100
# Nobody's chat wants a 19-minute prog epic bought for 100 points. Long
# enough for a genuine long song, short enough that the queue keeps moving.
DEFAULT_MAX_TRACK_SECONDS = 600
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10

# Spotify hands out an access token valid for an hour. Refreshed slightly
# early so a request that arrives in the last moments of its life does not
# race the expiry and 401.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60

_access_token: "str | None" = None
_access_token_expires_at: float = 0.0
# One refresh at a time. Without it, a burst of requests on a cold token
# would each start their own refresh, and Spotify would answer some of
# them with a token the others have already replaced.
_token_lock = asyncio.Lock()

# Anything Spotify says no to that is worth a specific sentence in chat.
# The rest fall through to a generic message - being wrong about which
# error this is would be worse than being vague.
_NO_ACTIVE_DEVICE = "NO_ACTIVE_DEVICE"


class SpotifyNotConfigured(Exception):
    """No refresh token yet - nobody has completed /auth/spotify/login."""


class SpotifyUnavailable(Exception):
    """
    Spotify could not be reached, or refused in a way the viewer cannot
    act on. Its own type so the caller knows the queue definitely did NOT
    happen and the points must go back.
    """


def is_configured() -> bool:
    return bool(config.get("spotify_refresh_token", ""))


def requests_enabled() -> bool:
    return bool(config.get("spotify_song_requests_enabled", True))


def request_cost() -> int:
    return int(config.get("spotify_request_cost", DEFAULT_REQUEST_COST))


def _timeout() -> "aiohttp.ClientTimeout":
    seconds = config.get("spotify_api_timeout_seconds", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS
    return aiohttp.ClientTimeout(total=seconds)


def forget_token() -> None:
    """Drops the cached access token. For the tests, and for the OAuth callback."""
    global _access_token, _access_token_expires_at
    _access_token = None
    _access_token_expires_at = 0.0


async def access_token() -> str:
    """
    A valid access token, refreshing it first if the cached one is gone or
    about to expire.
    """
    global _access_token, _access_token_expires_at

    async with _token_lock:
        if _access_token and time.time() < _access_token_expires_at:
            return _access_token

        refresh_token = config.get("spotify_refresh_token", "")
        client_id = config.get("spotify_client_id", "")
        client_secret = config.get("spotify_client_secret", "")
        if not refresh_token:
            raise SpotifyNotConfigured(
                "Spotify isn't connected yet - open /auth/spotify/login from the admin dashboard."
            )
        if not client_id or not client_secret:
            raise SpotifyNotConfigured("spotify_client_id and spotify_client_secret must be set in config.json.")

        # Basic auth with the client credentials, per Spotify's refresh
        # flow. The secret never leaves this process.
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        body = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        headers = {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            async with aiohttp.ClientSession(timeout=_timeout()) as session:
                async with session.post(TOKEN_URL, data=body, headers=headers) as resp:
                    data = await resp.json()
                    if resp.status != 200 or "access_token" not in data:
                        log.error(f"Spotify token refresh failed: {resp.status} {data}")
                        raise SpotifyUnavailable("Spotify refused the saved login - reconnect from the dashboard.")
        except asyncio.TimeoutError as e:
            raise SpotifyUnavailable("Spotify did not answer the token refresh in time.") from e
        except aiohttp.ClientError as e:
            raise SpotifyUnavailable(f"Could not reach Spotify: {e}") from e

        _access_token = data["access_token"]
        _access_token_expires_at = time.time() + int(data.get("expires_in", 3600)) - _TOKEN_EXPIRY_MARGIN_SECONDS
        # Spotify MAY return a new refresh token on refresh, and when it
        # does the old one stops working. Silently dropping it would mean
        # song requests worked until the next restart and then never again.
        if data.get("refresh_token"):
            config.set("spotify_refresh_token", data["refresh_token"])
            config.save()
            log.info("Spotify issued a new refresh token - saved")
        return _access_token


async def _api(method: str, path: str, **kwargs) -> "dict | None":
    """
    One Spotify Web API call. Returns the decoded body, or None for the
    204 responses the player endpoints answer with.
    """
    token = await access_token()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession(timeout=_timeout()) as session:
            async with session.request(method, f"{API_BASE}{path}", headers=headers, **kwargs) as resp:
                if resp.status == 204:
                    return None
                text = await resp.text()
                data = {}
                if text:
                    try:
                        data = await resp.json()
                    except Exception:
                        data = {}

                if resp.status == 403:
                    raise SpotifyUnavailable(
                        "Spotify refused this - song requests need Spotify Premium on the streamer's account."
                    )
                if resp.status == 404 and _NO_ACTIVE_DEVICE in text:
                    raise SpotifyUnavailable(
                        "Spotify isn't playing anything right now - it has to be open and playing to queue onto."
                    )
                if resp.status == 429:
                    raise SpotifyUnavailable("Spotify is rate-limiting us - try again in a moment.")
                if resp.status >= 400:
                    message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                    raise SpotifyUnavailable(f"Spotify said no ({resp.status}): {message or 'no reason given'}")
                return data
    except asyncio.TimeoutError as e:
        raise SpotifyUnavailable("Spotify didn't answer in time.") from e
    except aiohttp.ClientError as e:
        raise SpotifyUnavailable(f"Could not reach Spotify: {e}") from e


# A pasted link, a URI, or the bare id. Accepted alongside plain text
# search because pasting the link is what people actually do, and running
# a URL through the search endpoint finds nothing.
_TRACK_REFERENCE = re.compile(
    r"(?:spotify:track:|open\.spotify\.com/(?:intl-[a-z]{2}/)?track/)([A-Za-z0-9]{22})"
)


def track_id_from(text: str) -> "str | None":
    match = _TRACK_REFERENCE.search(text or "")
    return match.group(1) if match else None


def describe(track: dict) -> str:
    """"Artist - Title", the way a chat message wants it."""
    artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
    name = track.get("name", "this track")
    return f"{artists} - {name}" if artists else name


async def search_track(query: str) -> "dict | None":
    """The first track match, or None. market=from_token so results are playable on the streamer's account."""
    data = await _api(
        "GET",
        "/search",
        params={"q": query, "type": "track", "limit": "1", "market": "from_token"},
    )
    items = ((data or {}).get("tracks") or {}).get("items") or []
    return items[0] if items else None


async def get_track(track_id: str) -> "dict | None":
    return await _api("GET", f"/tracks/{track_id}", params={"market": "from_token"})


async def add_to_queue(uri: str) -> None:
    await _api("POST", "/me/player/queue", params={"uri": uri})


async def now_playing() -> "dict | None":
    """
    The current playback state - the track, how far into it, and whether it
    is actually playing - or None when Spotify has nothing loaded.

    The progress matters as much as the track: the widget draws a moving
    bar, and it gets one reading every few seconds, so it interpolates from
    this between polls rather than jumping.
    """
    data = await _api("GET", "/me/player/currently-playing", params={"market": "from_token"})
    if not data or not data.get("item"):
        return None
    return {
        "item": data["item"],
        "progress_ms": data.get("progress_ms") or 0,
        "is_playing": bool(data.get("is_playing")),
    }


DEFAULT_QUEUE_PREVIEW = 3

# Each entry is trimmed so three of them plus the "@name up next:" prefix
# fit inside YouTube's 200-character message limit. Chosen against real
# titles, not short ones: "An Artist With A Long Name - Some Reasonably
# Long Song Title (Remastered)" is 73 characters on its own, and three of
# those overflow before any separator is counted.
#
# Trimming each entry rather than listing fewer of them is the better
# trade: the question is "is mine soon", which two entries answer worse
# than three shortened ones do.
_QUEUE_ENTRY_MAX_CHARS = 42


async def queue() -> "list":
    """
    What is coming up next, soonest first.

    Spotify returns the currently playing track alongside the queue and
    this drops it - "what is playing" is the overlay's job, and repeating
    it as the first queue entry makes the chat answer look off by one.

    Needs only user-read-playback-state, which the consent flow already
    asks for, so this adds no re-authorisation.
    """
    data = await _api("GET", "/me/player/queue")
    return [item for item in ((data or {}).get("queue") or []) if isinstance(item, dict)]


def queue_preview(items: "list", limit: int) -> str:
    """
    The queue as one chat line.

    Kept short deliberately. YouTube drops a message over 200 characters
    silently (streamerbot_client splits, but a split queue listing is
    three messages of noise), and nobody reads past the next few anyway -
    the question being asked is "is mine soon", not "show me everything".
    """
    if not items:
        return "nothing queued"
    shown = items[:limit]
    parts = []
    for index, item in enumerate(shown, start=1):
        who = requester_of(item.get("uri", ""))
        title = describe(item)
        if len(title) > _QUEUE_ENTRY_MAX_CHARS:
            title = title[: _QUEUE_ENTRY_MAX_CHARS - 1].rstrip() + "\u2026"
        parts.append(f"{index}. {title}" + (f" ({who})" if who else ""))
    line = " | ".join(parts)
    remaining = len(items) - len(shown)
    return f"{line} (+{remaining} more)" if remaining > 0 else line


# Who asked for what, so the now-playing widget can say so - the one thing
# an off-the-shelf Spotify widget cannot do, because it has no idea a
# request system exists.
#
# Keyed by track URI rather than by queue position, because Spotify exposes
# no queue-position identity: the same track requested twice is the same
# key, which is right - whoever asked most recently gets the credit, and
# the alternative is remembering an order Spotify never promised to keep.
_REQUESTER_MEMORY = 50
_recent_requests: dict = {}


def remember_requester(uri: str, username: str) -> None:
    _recent_requests[uri] = username
    while len(_recent_requests) > _REQUESTER_MEMORY:
        # Python dicts keep insertion order, so the oldest entry is first.
        _recent_requests.pop(next(iter(_recent_requests)))


def requester_of(uri: str) -> "str | None":
    return _recent_requests.get(uri)


def forget_requesters() -> None:
    """For the tests."""
    _recent_requests.clear()


async def _resolve(query: str) -> "dict | None":
    track_id = track_id_from(query)
    if track_id:
        return await get_track(track_id)
    return await search_track(query)


async def request_song(username: str, query: str, platform: str = "twitch") -> dict:
    """
    The whole viewer-facing flow: resolve, charge, queue, refund on
    failure. Returns a result dict rather than raising, so the chat layer
    has one shape to render.

    The track is resolved BEFORE any points are taken. A viewer who typed
    a song that does not exist should not be charged and then refunded -
    that shows up in their balance history as two entries for nothing, and
    it makes a typo look like a system fault.
    """
    if not requests_enabled():
        return {"ok": False, "reason": "Song requests are switched off right now"}
    if not is_configured():
        return {"ok": False, "reason": "Song requests aren't set up yet"}
    if not query.strip():
        return {"ok": False, "reason": f"Give me a song - !song <name or Spotify link> (costs {request_cost()} points)"}

    try:
        track = await _resolve(query.strip())
    except (SpotifyNotConfigured, SpotifyUnavailable) as e:
        return {"ok": False, "reason": str(e)}
    if not track:
        return {"ok": False, "reason": f"Couldn't find \"{query.strip()}\" on Spotify"}

    max_seconds = int(config.get("spotify_max_track_seconds", DEFAULT_MAX_TRACK_SECONDS))
    duration_seconds = int(track.get("duration_ms", 0) / 1000)
    if duration_seconds > max_seconds:
        return {
            "ok": False,
            "reason": f"{describe(track)} is {duration_seconds // 60} minutes - the limit is {max_seconds // 60}",
        }

    cost = request_cost()
    if cost > 0:
        try:
            paid, balance = await try_spend(username, cost, platform=platform)
        except UnknownUser:
            return {"ok": False, "reason": "I can't find your points account - say something in chat first, then retry"}
        except Exception as e:
            log.warning(f"Song request by {username} failed at the points step: {e}")
            return {"ok": False, "reason": "Couldn't take your points right now - try again in a moment"}
        if not paid:
            have = "you have 0" if balance == 0 else (f"you have {balance}" if balance is not None else "you don't have enough")
            return {"ok": False, "reason": f"A song request costs {cost} points - {have}"}

    try:
        await add_to_queue(track["uri"])
    except Exception as e:
        # Paid for, not queued. This is the branch the refund exists for.
        log.warning(f"Song request by {username} was charged but not queued: {e}")
        if cost > 0:
            try:
                await grant_points(username, cost, platform=platform)
                log.info(f"Refunded {cost} points to {username} - the track could not be queued")
            except Exception:
                log.error(
                    f"REFUND FAILED: {username} paid {cost} points for a song request that was not queued, "
                    f"and the points could not be returned. Give them back by hand."
                )
        reason = str(e) if isinstance(e, (SpotifyUnavailable, SpotifyNotConfigured)) else "Spotify wouldn't take it"
        return {"ok": False, "reason": f"{reason} - your points are back"}

    remember_requester(track["uri"], username)
    log.info(f"{username} queued {describe(track)} for {cost} points")
    return {"ok": True, "track": track, "description": describe(track), "cost": cost}


async def handle_chat_command(event: dict) -> None:
    """
    Registered via streamerbot.on_event() from main.py, alongside the
    roulette's own handler. Separate rather than folded into that one
    because these are unrelated features that happen to read the same
    stream, and the roulette's dispatcher is already the longest thing in
    that file.
    """
    from streamerbot_client import parse_chat_message, streamerbot

    chat = parse_chat_message(event)
    if chat is None:
        return

    text = (chat["text"] or "").strip()
    username = chat["username"]
    platform = chat["platform"]
    if not text.startswith("!") or not username:
        return

    parts = text[1:].split(maxsplit=1)
    if not parts:
        return
    command = parts[0].lower()
    argument = parts[1] if len(parts) > 1 else ""

    async def reply(message: str) -> None:
        if config.get("spotify_chat_replies_enabled", True):
            await streamerbot.send_chat_message(message, platform=platform)

    # !song is the REQUEST command, not a "what's playing" one. What is
    # playing has its own overlay on stream, and a chat command that
    # answers a question the viewer can already see is a command spent on
    # nothing - where !song is the word people reach for when they want to
    # ask for one.
    if command in ("songqueue", "queue", "songs"):
        # Free, like the overlay it complements. A viewer who has just
        # paid to queue something asks this to find out how long they are
        # waiting, and charging for that answer would be charging twice
        # for the same request.
        try:
            upcoming = await queue()
        except (SpotifyNotConfigured, SpotifyUnavailable) as e:
            await reply(f"@{username} {e}")
            return
        limit = int(config.get("spotify_queue_preview_count", DEFAULT_QUEUE_PREVIEW))
        await reply(f"@{username} up next: {queue_preview(upcoming, limit)}")
    elif command in ("song", "sr", "songrequest", "request"):
        result = await request_song(username, argument, platform=platform)
        if result.get("ok"):
            await reply(f"@{username} queued {result['description']}")
        else:
            await reply(f"@{username} {result['reason']}")


def status() -> dict:
    """For the admin dashboard's status panel."""
    return {
        "configured": is_configured(),
        "requests_enabled": requests_enabled(),
        "request_cost": request_cost(),
        # Whether the in-memory access token is currently valid. Not a
        # health check - it says only that a refresh happened recently and
        # worked, which is still the difference between "connected" and
        # "connected once, months ago, and the token was revoked since".
        "token_fresh": bool(_access_token and time.time() < _access_token_expires_at),
    }


# ---------- Now-playing, pushed to the overlay ----------

DEFAULT_POLL_SECONDS = 5

_poller_task: "asyncio.Task | None" = None


def _poll_seconds() -> float:
    try:
        return max(1.0, float(config.get("spotify_now_playing_poll_seconds", DEFAULT_POLL_SECONDS)))
    except (TypeError, ValueError):
        return float(DEFAULT_POLL_SECONDS)


def now_playing_payload(state: "dict | None") -> dict:
    """
    The message the overlay renders, built from a playback state.

    Sent in full every tick rather than only on change. The overlay is a
    Browser Source that can be reloaded at any moment and the hub has no
    "send me the current state" call, so a change-only stream would leave
    a widget blank after every refresh until the next song - which is the
    exact limitation forced-buy-badge.js documents and has to live with.
    Repeating a small payload every few seconds costs nothing and removes
    the whole problem.
    """
    if not state or not state.get("item"):
        return {"type": "now_playing", "playing": False, "track": None}

    item = state["item"]
    images = (item.get("album") or {}).get("images") or []
    # Spotify returns images largest-first. The smallest is still 64px,
    # which is plenty at overlay size and is the one worth fetching.
    art = images[-1].get("url") if images else None
    return {
        "type": "now_playing",
        "playing": bool(state.get("is_playing")),
        "track": {
            "title": item.get("name", ""),
            "artists": ", ".join(a.get("name", "") for a in item.get("artists", []) if a.get("name")),
            "album_art": art,
            "duration_ms": item.get("duration_ms", 0),
            "url": (item.get("external_urls") or {}).get("spotify"),
        },
        "progress_ms": state.get("progress_ms", 0),
        # None for anything the streamer put on themselves, which is most
        # of a stream - the overlay simply omits the line rather than
        # inventing a requester.
        "requested_by": requester_of(item.get("uri", "")),
    }


async def _poll_once() -> None:
    from widget_hub import widget_hub

    # Nothing is listening, so nothing is worth an API call. Spotify rate
    # limits, and a backend left running overnight with no overlay open
    # would otherwise spend the night asking what is playing.
    if widget_hub.connected_count("spotify") == 0:
        return
    if not is_configured():
        return

    state = await now_playing()
    await widget_hub.broadcast(now_playing_payload(state), tag="spotify")


async def _poll_loop() -> None:
    while True:
        try:
            await _poll_once()
        except (SpotifyNotConfigured, SpotifyUnavailable) as e:
            # Expected and transient - Spotify closed, token being
            # refreshed, network blip. Logged at debug so a stream's worth
            # of them cannot bury anything that matters.
            log.debug(f"Now-playing poll skipped: {e}")
        except Exception:
            log.exception("Now-playing poll failed unexpectedly")
        await asyncio.sleep(_poll_seconds())


async def start_now_playing_poller() -> None:
    """Starts the overlay feed. Safe to call twice - the second call is a no-op."""
    global _poller_task
    if _poller_task and not _poller_task.done():
        return
    _poller_task = asyncio.create_task(_poll_loop())
    log.info("Now-playing poller started")


async def stop_now_playing_poller() -> None:
    global _poller_task
    if _poller_task:
        _poller_task.cancel()
        _poller_task = None
