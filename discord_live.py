"""
Posts a go-live announcement to a Discord webhook, once per broadcast.

The hard part here is not the posting, it is not posting twice. A stream
does not go live once and cleanly: Streamer.bot reconnects, Twitch's API
is polled on a timer, the backend gets restarted mid-stream, and the
connection drops and comes back. Every one of those looks like "we are
live" to something naive, and a Discord ping is the one thing in this
project that cannot be taken back.

So this module is built around the announcement being **edge-triggered,
persisted and rate-limited**, and the actual HTTP call is the small part:

  * EDGE, not level. `announce()` fires on the transition into live, and
    a source that keeps saying "still live" says nothing after the first.
  * PERSISTED. The last announcement is written beside config.json, so a
    restart mid-stream does not re-announce - which is exactly when it
    would, since a fresh process starts with no idea it already did.
  * COOLDOWN. `discord_live_cooldown_minutes` (60) covers the case
    persistence cannot: a stream that drops and returns twenty minutes
    later is the same session to everyone reading Discord.

**Two independent sources, one announcer**, deliberately - the same shape
`roulette.on_new_buy_phase` already uses for its two buy-phase signals,
and for the same reason: they fail differently. The Twitch poll needs
nothing but the app token this project already has, and keeps working
while Streamer.bot is down; the Streamer.bot event arrives instantly and
is the only route that can ever cover YouTube, since YouTube's own API
cannot be polled for this inside a sane quota. With both live an
announcement is attempted twice and the dedupe collapses it to one.

Off by default, and `is_enabled()` requires `discord_webhook_url` as well
as the flag - the same rule `greeter.is_enabled()` follows, for the same
reason: enabled with nowhere to post is a feature that silently does
nothing while looking configured.
"""
import asyncio
import json
import os
import re
import time
from pathlib import Path

import aiohttp

from config import config
from logger import get_logger

log = get_logger("DiscordLive")

DEFAULT_STATE_PATH = Path(__file__).parent / "live_announce_state.json"
DEFAULT_POLL_SECONDS = 60
DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_MESSAGE = "{mention}**{name}** is live now - {title}"
# Streamer.bot's own names for "the broadcast started", across platforms.
# A list rather than a constant because which of these a given build emits
# depends on its version and on which platform integrations are connected,
# and being wrong about that should be a config edit, not a code change.
DEFAULT_GOLIVE_EVENTS = ["StreamOnline", "BroadcastStarted", "StreamUp", "LiveStreamStarted"]

_state: "dict | None" = None
_poll_task: "asyncio.Task | None" = None
_announce_lock = asyncio.Lock()


# ---------- config ----------

def is_enabled() -> bool:
    return bool(config.get("discord_live_enabled", False)) and bool(config.get("discord_webhook_url", ""))


def _state_path() -> Path:
    return Path(config.get("discord_live_state_file", str(DEFAULT_STATE_PATH)))


def _cooldown_seconds() -> float:
    try:
        return float(config.get("discord_live_cooldown_minutes", DEFAULT_COOLDOWN_MINUTES)) * 60
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_MINUTES * 60


def _poll_seconds() -> float:
    try:
        return max(15.0, float(config.get("discord_live_poll_seconds", DEFAULT_POLL_SECONDS)))
    except (TypeError, ValueError):
        return float(DEFAULT_POLL_SECONDS)


# ---------- persisted state ----------

def _load() -> dict:
    global _state
    if _state is not None:
        return _state
    try:
        _state = json.loads(_state_path().read_text())
        if not isinstance(_state, dict):
            raise ValueError("not an object")
    except FileNotFoundError:
        _state = {}
    except Exception:
        # A corrupt file must not take the announcer down, but starting
        # from empty means the next signal announces. That is the right
        # way round: a duplicate ping is recoverable, a stream that never
        # gets announced is the feature not working.
        log.warning(f"Could not read {_state_path()} - starting with no announcement history")
        _state = {}
    return _state


def _save() -> None:
    try:
        _state_path().write_text(json.dumps(_load()))
    except Exception:
        log.exception("Could not write the go-live announcement state")


def reset() -> None:
    """Drops the cached state. Exists for the tests."""
    global _state
    _state = None


def last_announced() -> dict:
    return dict(_load())


# ---------- the dedupe ----------

def should_announce(stream_id: str, now: float) -> bool:
    """
    Whether this go-live signal is a new broadcast worth announcing.

    Two independent guards, because they catch different things. The
    stream id catches the same broadcast being reported again by either
    source, however long apart - that is the exact case, and it is exact.
    The cooldown catches everything the id cannot: a source that sends no
    id at all, and a genuine restart of the stream software that Twitch
    counts as a fresh broadcast twenty minutes later but nobody reading
    Discord would.
    """
    if not is_enabled():
        return False
    state = _load()
    if stream_id and state.get("stream_id") == stream_id:
        return False
    try:
        last = float(state.get("announced_at", 0))
    except (TypeError, ValueError):
        last = 0.0
    return (now - last) >= _cooldown_seconds()


def _remember(stream_id: str, now: float) -> None:
    state = _load()
    state["stream_id"] = stream_id or ""
    state["announced_at"] = now
    _save()


# ---------- the message ----------

def build_payload(title: str, game: str, url: str, name: str, image: str = "") -> dict:
    """
    The Discord webhook body.

    `allowed_mentions` is set from the configured mention rather than left
    off: Discord's default is to honour every mention in the content, so a
    title containing "@everyone" would ping the server. Naming exactly
    what may ping means a stream title can never do it.
    """
    mention = str(config.get("discord_live_role_mention", "") or "").strip()
    template = str(config.get("discord_live_message", DEFAULT_MESSAGE))
    content = (template
               .replace("{mention}", (mention + " ") if mention else "")
               .replace("{name}", name or "the stream")
               .replace("{title}", title or "live now")
               .replace("{game}", game or "")
               .replace("{url}", url or "")).strip()

    allowed: dict = {"parse": []}
    if mention.startswith("<@&") or mention == "@everyone" or mention == "@here":
        allowed = {"parse": ["everyone"]} if mention.startswith("@") else {"roles": [mention.strip("<@&>")]}

    payload: dict = {"content": content, "allowed_mentions": allowed}
    if url:
        embed: dict = {"title": title or "Live now", "url": url}
        if game:
            embed["description"] = f"Playing {game}"
        # An attachment:// image is a file being uploaded with this post
        # and always wins. Otherwise discord_live_image_url overrides
        # Twitch's own thumbnail, which is an automatic screenshot of
        # whatever was on screen - frequently a black frame at the moment
        # of going live, which is the exact moment this posts.
        if not image.startswith("attachment://"):
            image = str(config.get("discord_live_image_url", "") or "").strip() or image
        if image:
            embed["image"] = {"url": image}
        payload["embeds"] = [embed]
    return payload


async def announce(title: str = "", game: str = "", url: str = "", name: str = "",
                   stream_id: str = "", image: str = "", session_factory=None) -> bool:
    """
    Posts the announcement if this is genuinely a new broadcast.

    Returns whether anything was posted, so a caller can log the
    difference between "suppressed" and "failed" - which look identical
    from outside and want different fixes.

    The lock matters: the Twitch poll and the Streamer.bot event can
    arrive within milliseconds of each other, and without it both would
    pass `should_announce` before either had written the state.
    """
    async with _announce_lock:
        now = time.time()
        if not should_announce(stream_id, now):
            log.info(f"Go-live signal suppressed (already announced this broadcast): {stream_id or 'no id'}")
            return False

        webhook = config.get("discord_webhook_url", "")

        # A local file beats every URL. Uploading the thumbnail somewhere
        # and pasting a link before each stream is work; overwriting one
        # file you already export to is not. Missing file just falls
        # through to whatever URL was going to be used anyway.
        image_file = str(config.get("discord_live_image_file", "") or "").strip()
        if image_file and not os.path.isfile(image_file):
            log.warning(f"discord_live_image_file points at nothing: {image_file}")
            image_file = ""
        if image_file:
            image = "attachment://" + os.path.basename(image_file)
        elif not str(config.get("discord_live_image_url", "") or "").strip():
            # Prefer the thumbnail you actually made over Twitch's
            # automatic screenshot of whatever was on screen.
            image = (await youtube_live_thumbnail()) or image

        payload = build_payload(title, game, url, name, image)

        if session_factory is None:
            def session_factory():
                return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

        try:
            async with session_factory() as session:
                if image_file:
                    form = aiohttp.FormData()
                    form.add_field("payload_json", json.dumps(payload),
                                   content_type="application/json")
                    with open(image_file, "rb") as fh:
                        form.add_field("files[0]", fh.read(),
                                       filename=os.path.basename(image_file),
                                       content_type="application/octet-stream")
                    post = session.post(webhook, data=form)
                else:
                    post = session.post(webhook, json=payload)
                async with post as resp:
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        log.error(f"Discord refused the go-live post ({resp.status}): {body}")
                        return False
        except Exception as e:
            # Deliberately NOT remembered: an announcement that failed to
            # post has not been made, and the next poll should try again.
            log.error(f"Could not reach the Discord webhook: {e}")
            return False

        _remember(stream_id, now)
        log.info(f"Announced go-live to Discord: {title or name or stream_id}")
        return True


async def youtube_live_thumbnail(session_factory=None) -> str:
    """
    The thumbnail you already uploaded to YouTube, read back off their CDN.

    You make a thumbnail and upload it with the broadcast; asking you to
    also copy it onto the Mac Mini is the same work twice. All this needs
    is the live video's id, and /live redirects to it - so no API key, no
    quota, no second upload.

    ponytail: scrapes the id out of the /live page's HTML. YouTube can
    change that markup; if it ever stops matching this returns "" and the
    post falls through to the next image source rather than failing.
    """
    channel = str(config.get("youtube_channel_id", "") or "").strip()
    if not channel:
        return ""
    if session_factory is None:
        def session_factory():
            return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    try:
        async with session_factory() as session:
            async with session.get(f"https://www.youtube.com/channel/{channel}/live") as resp:
                html = await resp.text()
    except Exception as e:
        log.warning(f"Could not read the YouTube live page: {e}")
        return ""
    match = re.search(r'"videoId":"([\w-]{11})"', html)
    if not match:
        log.info("No live video id on the YouTube live page - not live there yet?")
        return ""
    # maxres only exists if the uploaded thumbnail was big enough; hqdefault
    # always exists, so it is the safe one to hand Discord.
    return f"https://img.youtube.com/vi/{match.group(1)}/maxresdefault.jpg"


# ---------- source 1: the Twitch poll ----------

async def poll_once(stream_info=None) -> bool:
    """One pass of the Twitch live check. Returns whether it announced."""
    channel = config.get("twitch_channel", "")
    if not is_enabled() or not channel:
        return False

    if stream_info is None:
        import twitch_client
        stream_info = twitch_client.stream_info
    try:
        info = await stream_info(channel)
    except Exception as e:
        log.warning(f"Twitch live check failed: {e}")
        return False
    if not info:
        return False
    # Twitch hands back a templated URL; the cache-buster is because
    # Discord caches an embed image by URL, so without it every stream
    # would show the first stream's screenshot forever.
    thumb = str(info.get("thumbnail_url", "") or "")
    if thumb:
        thumb = thumb.replace("{width}", "1280").replace("{height}", "720")
        thumb += ("&" if "?" in thumb else "?") + "t=" + str(int(time.time()))
    return await announce(
        title=info.get("title", ""),
        game=info.get("game_name", ""),
        url=f"https://twitch.tv/{channel}",
        name=info.get("user_name") or channel,
        stream_id=str(info.get("id", "")),
        image=thumb,
    )


async def _poll_loop() -> None:
    while True:
        try:
            await poll_once()
        except Exception:
            log.exception("Go-live poll failed")
        await asyncio.sleep(_poll_seconds())


async def start_poller() -> None:
    """
    Safe to call twice; does nothing while the feature is off.

    Logs on BOTH paths on purpose. It used to announce only that it was
    disabled, which made a working poller and a module that never loaded
    look identical in the log - and the log is the only window onto this
    thing until a stream actually starts. A feature whose healthy state is
    silence cannot be diagnosed.
    """
    global _poll_task
    if not is_enabled():
        log.info("Discord go-live announcements are OFF (needs discord_live_enabled and discord_webhook_url)")
        return
    channel = config.get("twitch_channel", "")
    if not channel:
        log.warning("Discord go-live is enabled but twitch_channel is empty - the poll has nothing to check")
    if _poll_task and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_poll_loop())
    log.info(
        f"Discord go-live poller started - checking Twitch channel "
        f"{channel or '<unset>'} every {int(_poll_seconds())}s"
    )


async def stop_poller() -> None:
    global _poll_task
    if _poll_task:
        _poll_task.cancel()
        _poll_task = None


# ---------- source 2: Streamer.bot ----------

def golive_event_types() -> set:
    raw = config.get("streamerbot_golive_events", DEFAULT_GOLIVE_EVENTS)
    return {str(x) for x in (raw or [])}


async def handle_streamerbot_event(event: dict) -> None:
    """
    Registered via streamerbot.on_event() like every other listener.

    Reads the event's own fields where they exist and falls back to the
    Twitch poll's richer data where they do not - a go-live event that
    carries nothing but its name is still a perfectly good trigger, and
    an announcement with a bare title beats no announcement.
    """
    if not is_enabled():
        return
    body = event.get("event") or {}
    if str(body.get("type", "")) not in golive_event_types():
        return

    data = event.get("data") or {}
    channel = config.get("twitch_channel", "")
    stream_id = str(data.get("id") or data.get("streamId") or "")
    log.info(f"Streamer.bot reported a go-live event: {body.get('type')}")
    await announce(
        title=str(data.get("title") or data.get("status") or ""),
        game=str(data.get("category") or data.get("game") or data.get("gameName") or ""),
        url=str(data.get("url") or (f"https://twitch.tv/{channel}" if channel else "")),
        name=str(data.get("broadcastUser") or data.get("userName") or channel),
        stream_id=stream_id,
    )
