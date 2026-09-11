"""
Greets a first-time chatter once, with the Discord link.

The pattern this replaces is Nightbot's timer: drop the invite into chat
every N minutes as long as people are talking. That works, and it also
means the regulars - the people who already joined - read the same link
forty times a stream. Firing on a viewer's FIRST message instead puts it
in front of exactly the people it is for, once each, and says nothing to
anyone else.

**"First" means first ever, not first this stream**, so it is persisted.
A list of who has spoken lives beside config.json; a restart mid-stream
must not re-welcome the whole chat, which is precisely the moment it
would look broken.

**Streamer.bot's own first-message flag is deliberately not the
mechanism.** Twitch sends a `first-msg` tag and Streamer.bot surfaces it,
but YouTube sends nothing of the kind, and this stack is dual-platform -
building on the flag would mean the feature silently working on half the
audience. The seen-list works identically on both. The flag is still read
when it happens to be there, as a way to catch someone whose name this
backend has simply never seen because it was not running the first time
they spoke.

**A global cooldown, not a per-user one.** Per-user is already handled by
only ever greeting someone once; the cooldown exists for the raid, where
forty first-time chatters arrive in ten seconds and forty invite links
would be the worst possible welcome. Everyone past the first in that
window is recorded as seen and simply not greeted - that is the deliberate
trade, because a quiet welcome is better than a flooded chat.
"""
import json
import time
from pathlib import Path

from config import config
from logger import get_logger

log = get_logger("Greeter")

DEFAULT_SEEN_PATH = Path(__file__).parent / "seen_chatters.json"
DEFAULT_COOLDOWN_SECONDS = 45
DEFAULT_MESSAGE = "welcome in! Come hang out in the Discord: {discord}"

_seen: "set | None" = None
_last_greeted_at: float = 0.0


def _seen_path() -> Path:
    return Path(config.get("greeter_seen_file", str(DEFAULT_SEEN_PATH)))


def _key(platform: str, username: str) -> str:
    """
    Platform-scoped, because the same handle on Twitch and YouTube is not
    reliably the same person - and greeting someone twice is a far smaller
    cost than never greeting a real newcomer because their name collides.
    """
    return f"{(platform or '').lower()}:{(username or '').lower()}"


def _load() -> set:
    global _seen
    if _seen is not None:
        return _seen
    path = _seen_path()
    try:
        _seen = set(json.loads(path.read_text()))
    except FileNotFoundError:
        _seen = set()
    except Exception:
        # A corrupt file must not take chat down with it. Starting from
        # empty re-greets people once, which is a far better failure than
        # the handler raising on every message.
        log.warning(f"Could not read {path} - starting the seen list empty")
        _seen = set()
    return _seen


def _save() -> None:
    try:
        _seen_path().write_text(json.dumps(sorted(_load())))
    except Exception:
        # Worth a line, not worth failing over: the in-memory set is still
        # correct for this session, so the only cost is re-greeting after
        # a restart.
        log.exception("Could not write the seen-chatters list")


def has_seen(platform: str, username: str) -> bool:
    return _key(platform, username) in _load()


def remember(platform: str, username: str) -> None:
    _load().add(_key(platform, username))
    _save()


def reset() -> None:
    """Drops the in-memory list. Exists for the tests."""
    global _seen, _last_greeted_at
    _seen = None
    _last_greeted_at = 0.0


def is_enabled() -> bool:
    return bool(config.get("greeter_enabled", False)) and bool(config.get("discord_invite_url", ""))


def _ignored(username: str) -> bool:
    ignored = config.get("greeter_ignore_users", []) or []
    return (username or "").lower() in {str(u).lower() for u in ignored}


def greeting_for(username: str) -> str:
    template = config.get("greeter_message", DEFAULT_MESSAGE)
    return f"@{username} " + str(template).replace("{discord}", config.get("discord_invite_url", ""))


def _cooldown_seconds() -> float:
    try:
        return float(config.get("greeter_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS))
    except (TypeError, ValueError):
        return float(DEFAULT_COOLDOWN_SECONDS)


def should_greet(platform: str, username: str, now: float, first_message_flag: bool = False) -> bool:
    """
    Whether this message earns a greeting.

    `first_message_flag` is Twitch's own `first-msg`, when Streamer.bot
    passes it through. It can only ADD a greeting, never suppress one: a
    name this backend has never recorded is new to us regardless of what
    the platform thinks, and the flag is absent entirely on YouTube.
    """
    if not is_enabled() or not username or _ignored(username):
        return False
    if has_seen(platform, username) and not first_message_flag:
        return False
    return (now - _last_greeted_at) >= _cooldown_seconds()


async def handle_chat_command(event: dict) -> None:
    """
    Registered via streamerbot.on_event() from main.py.

    Named like the other chat listeners for consistency, though it reacts
    to every message rather than to a command - which is the point.
    """
    global _last_greeted_at
    from streamerbot_client import parse_chat_message, streamerbot

    chat = parse_chat_message(event)
    if chat is None or not chat["username"]:
        return

    platform = chat["platform"]
    username = chat["username"]
    data = event.get("data") or {}
    first_flag = bool(data.get("firstMessage") or data.get("isFirstMessage"))

    now = time.time()
    if not should_greet(platform, username, now, first_flag):
        # Recorded even when not greeted. Someone who arrived mid-raid has
        # still been seen, and greeting them an hour later - when the
        # cooldown has long expired and they are no longer new - would be
        # stranger than not greeting them at all.
        if is_enabled() and username:
            remember(platform, username)
        return

    remember(platform, username)
    _last_greeted_at = now
    log.info(f"Greeting first-time chatter {username} on {platform}")
    await streamerbot.send_chat_message(
        greeting_for(chat["display_name"] or username), platform=platform or "twitch"
    )
