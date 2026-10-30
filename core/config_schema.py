"""
Setting metadata, secret masking and write validation for /api/config.

One source for the settings page: FIELDS describes every key in
config.example.json; keys it does not know come from `_custom_fields` or are
inferred. Secrets never leave the server - GET sends SECRET_MASK instead, and
a PUT carrying the mask back is treated as "unchanged".
"""
import math
import re

SECRET_MASK = "•" * 8
FIELD_TYPES = ("number", "text", "bool", "list", "table", "json", "secret")
SECTIONS = [
    "Streamer.bot", "Points (Cloudbot)", "Roulette", "Credit OCR", "Spotify",
    "Rank (!rank)", "Greeter", "Discord go-live", "Twitch & YouTube",
    "Break screen", "Login & site", "Editor app", "Other",
]

_SECRET_RE = re.compile(r"secret|token|password|api_key|webhook", re.I)
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# key: (section, type, label, description, extras)
# extras "needed": True means the settings page flags the key while it is empty;
# a key name instead flags it only while that bool setting is on.
_SB, _PT, _RL, _OC, _SP, _RK, _GR, _DL, _TY, _BR, _LS, _ED = SECTIONS[:12]
_ROWS = {
    "http_host": (_LS, "text", "Listen address", "Network address the backend binds to; 0.0.0.0 means every interface."),
    "http_port": (_LS, "number", "Listen port", "TCP port the backend serves on. Restart to apply.", {"integer": True, "min": 1024, "max": 65535}),
    "streamerbot_ws_url": (_SB, "text", "WebSocket URL", "Streamer.bot WebSocket address; must be the gaming PC's LAN address, not localhost.", {"needed": True}),
    "streamerbot_ws_password": (_SB, "secret", "WebSocket password", "Password for Streamer.bot's WebSocket authentication.", {"needed": True}),
    "streamerbot_send_as_bot": (_SB, "bool", "Send chat as bot", "Send chat replies from the bot account instead of the broadcaster."),
    "streamerbot_log_raw_chat": (_SB, "bool", "Log raw chat events", "Write every whole chat event to the log, to learn what a Streamer.bot build sends."),
    "streamerbot_subscribe_events": (_SB, "json", "Subscribed events", "Streamer.bot events to subscribe to, per platform."),
    "streamlabs_client_id": (_TY, "text", "Streamlabs client ID", "Client ID of the Streamlabs OAuth app."),
    "streamlabs_client_secret": (_TY, "secret", "Streamlabs client secret", "Client secret of the Streamlabs OAuth app."),
    "streamlabs_redirect_uri": (_TY, "text", "Streamlabs redirect URI", "OAuth callback URL registered with the Streamlabs app."),
    "streamlabs_access_token": (_TY, "secret", "Streamlabs access token", "Streamlabs access token, written by the Connect flow."),
    "streamlabs_refresh_token": (_TY, "secret", "Streamlabs refresh token", "Streamlabs refresh token, written by the Connect flow."),
    "streamlabs_channel": (_TY, "text", "Streamlabs channel", "Streamer's channel name; also the default user allowed to set the roulette agent."),
    "cloudbot_reply_timeout_seconds": (_PT, "number", "Cloudbot reply timeout", "How long to wait for Cloudbot to answer a points command.", {"unit": "seconds"}),
    "cloudbot_reply_usernames": (_PT, "list", "Cloudbot account names", "Chat names Cloudbot replies from; replies from anyone else are ignored. Empty accepts any sender (not recommended)."),
    "cloudbot_cache_ttl_seconds": (_PT, "number", "Balance cache time", "How long a viewer's balance read from Cloudbot is reused.", {"unit": "seconds"}),
    "points_exchange_rate_per_inr": (_PT, "number", "Points per rupee", "Points granted per rupee of a tip.", {"unit": "points"}),
    "streamlabs_tips_listener_enabled": (_PT, "bool", "Pay points for tips", "Grant points for Streamlabs tips; leave off while Cloudbot pays donors itself."),
    "roulette_trigger_cost": (_RL, "number", "Trigger cost", "Points a viewer pays to start a roulette.", {"unit": "points"}),
    "roulette_vote_cost_increment": (_RL, "number", "Vote cost step", "Extra points each further vote on the same weapon costs.", {"unit": "points"}),
    "roulette_voting_duration_seconds": (_RL, "number", "Voting window", "How long chat can vote once a roulette starts.", {"unit": "seconds"}),
    "roulette_cooldown_seconds": (_RL, "number", "Cooldown", "Minimum time between two roulettes.", {"unit": "seconds"}),
    "roulette_chat_replies_enabled": (_RL, "bool", "Chat replies", "Answer roulette commands in chat."),
    "roulette_affordability_filter_enabled": (_RL, "bool", "Affordability filter", "Only offer weapons the OCR credit reading says can be bought."),
    "roulette_random_pick_when_no_votes": (_RL, "bool", "Random pick on no votes", "Draw a random weapon when nobody voted."),
    "roulette_weapon_base_costs": (_RL, "table", "Vote cost per weapon", "Per-weapon base vote cost in points, overriding the default."),
    "roulette_base_wheel_share": (_RL, "number", "Base wheel share", "Wheel share every weapon starts with before votes."),
    "roulette_hide_pistols_off_pistol_rounds": (_RL, "bool", "Hide pistols off pistol rounds", "Drop most sidearms from the wheel on non-pistol rounds."),
    "roulette_pistol_weapons": (_RL, "list", "Pistols", "Weapons counted as sidearms."),
    "roulette_always_votable_pistols": (_RL, "list", "Always-votable pistols", "Sidearms kept on the wheel even when pistols are hidden."),
    "roulette_shield_reserve_creds": (_RL, "number", "Shield reserve", "Credits held back for armour when working out what is affordable.", {"unit": "credits"}),
    "roulette_ability_reserve_creds": (_RL, "number", "Ability reserve", "Credits held back for abilities when the agent is unknown.", {"unit": "credits"}),
    "roulette_pistol_round_max_creds": (_RL, "number", "Pistol round ceiling", "At or below this credit reading the round counts as a pistol round.", {"unit": "credits"}),
    "roulette_pistol_reserved_creds": (_RL, "number", "Pistol round reserve", "Credits held back on a pistol round.", {"unit": "credits"}),
    "roulette_current_agent": (_RL, "text", "Current agent", "Agent being played, used to size the ability reserve."),
    "roulette_agent_command_users": (_RL, "json", "Agent command users", "Usernames allowed to set the agent; null means only the streamer. A bare name is a Twitch login; write youtube:<name> for a YouTube one."),
    "roulette_agent_ability_costs": (_RL, "table", "Agent ability costs", "Credits to reserve for each agent's abilities."),
    "forced_buy_queued_duration_seconds": (_RL, "number", "Forced-buy queued badge", "How long the forced-buy badge shows as queued.", {"unit": "seconds"}),
    "forced_buy_active_duration_seconds": (_RL, "number", "Forced-buy active badge", "How long the forced-buy badge stays active.", {"unit": "seconds"}),
    "ocr_agent_secret": (_OC, "secret", "Agent shared secret", "Secret the gaming PC agent sends with credit readings and heartbeats.", {"needed": True}),
    "ocr_workers": (_OC, "number", "OCR workers", "Parallel Tesseract workers; restart to apply.", {"min": 1}),
    "ocr_reading_max_age_seconds": (_OC, "number", "Reading max age", "Backstop after which a credit reading is discarded.", {"unit": "seconds"}),
    "github_client_id": (_LS, "text", "GitHub client ID", "Client ID of the GitHub OAuth app used for admin login.", {"needed": True}),
    "github_client_secret": (_LS, "secret", "GitHub client secret", "Client secret of the GitHub OAuth app used for admin login.", {"needed": True}),
    "authorized_github_usernames": (_LS, "list", "Admin GitHub users", "GitHub usernames allowed into the admin dashboard.", {"needed": True}),
    "editor_github_username": (_ED, "text", "Editor GitHub user", "GitHub username allowed into the editor app only.", {"needed": True}),
    "gdrive_client_id": (_ED, "text", "Drive client ID", "Client ID of the Google Cloud OAuth app used for Google Drive.", {"needed": True}),
    "gdrive_client_secret": (_ED, "secret", "Drive client secret", "Client secret of the Google Cloud OAuth app used for Google Drive.", {"needed": True}),
    "gdrive_redirect_uri": (_ED, "text", "Drive redirect URI", "OAuth callback URL registered with the Google Cloud app.", {"needed": True}),
    "gdrive_refresh_token": (_ED, "secret", "Drive refresh token", "Google Drive refresh token, written by the Drive connect flow.", {"needed": True}),
    "editor_google_email": (_ED, "text", "Editor Google account", "The editor's Google account, which VODs and the brand kit are shared with.", {"needed": True}),
    "editor_discord_user_id": (_ED, "text", "Editor Discord user ID", "The editor's Discord user ID, which reminders and notifications are DMed to.", {"needed": "discord_notifications_enabled"}),
    "discord_bot_token": (_ED, "secret", "Discord bot token", "The bot token from the Discord Developer Portal, used to send DMs.", {"needed": "discord_notifications_enabled"}),
    "streamer_discord_user_id": (_ED, "text", "Streamer Discord user ID", "The streamer's Discord user ID, for new-version DMs.", {"needed": "discord_notifications_enabled"}),
    "discord_notifications_enabled": (_ED, "bool", "Discord notifications", "Send Discord DMs; the bell works either way."),
    "public_base_url": (_LS, "text", "Public URL", "Public address of this backend, used for the tunnel health check.", {"needed": True}),
    "spotify_client_id": (_SP, "text", "Client ID", "Client ID of the Spotify app.", {"needed": "spotify_song_requests_enabled"}),
    "spotify_client_secret": (_SP, "secret", "Client secret", "Client secret of the Spotify app.", {"needed": "spotify_song_requests_enabled"}),
    "spotify_refresh_token": (_SP, "secret", "Refresh token", "Spotify refresh token, written by the Connect flow.", {"needed": "spotify_song_requests_enabled"}),
    "spotify_redirect_uri": (_SP, "text", "Redirect URI", "OAuth callback URL registered with the Spotify app."),
    "spotify_song_requests_enabled": (_SP, "bool", "Song requests", "Let viewers queue songs with !song."),
    "spotify_chat_replies_enabled": (_SP, "bool", "Chat replies", "Answer song commands in chat."),
    "spotify_request_cost": (_SP, "number", "Request cost", "Points a viewer pays to queue a song.", {"unit": "points"}),
    "spotify_max_track_seconds": (_SP, "number", "Longest track", "Longest track a viewer may request.", {"unit": "seconds"}),
    "spotify_now_playing_poll_seconds": (_SP, "number", "Now-playing poll", "How often Spotify is asked what is playing.", {"unit": "seconds", "min": 1}),
    "spotify_queue_preview_count": (_SP, "number", "Queue preview length", "Tracks listed by !songqueue.", {"unit": "tracks"}),
    "break_default_minutes": (_BR, "number", "Default break length", "Break length preselected on the dashboard.", {"unit": "minutes", "min": 1}),
    "riot_name": (_RK, "text", "Riot name", "Riot ID name used for rank lookups.", {"needed": True}),
    "riot_tag": (_RK, "text", "Riot tag", "Riot ID tag, the part after the #.", {"needed": True}),
    "riot_region": (_RK, "text", "Region", "Valorant region for rank lookups, such as ap or eu."),
    "riot_platform": (_RK, "text", "Platform", "Valorant platform for rank lookups, such as pc."),
    "henrik_api_key": (_RK, "secret", "HenrikDev API key", "Key for the HenrikDev rank API.", {"needed": True}),
    "rank_cache_seconds": (_RK, "number", "Rank cache time", "How long a fetched rank is reused.", {"unit": "seconds"}),
    "greeter_enabled": (_GR, "bool", "Greeter", "Welcome first-time chatters with the Discord invite."),
    "discord_invite_url": (_GR, "text", "Discord invite", "Invite link the greeter posts; the greeter stays off without it.", {"needed": "greeter_enabled"}),
    "greeter_message": (_GR, "text", "Greeting", "Message posted to a newcomer; {discord} becomes the invite link."),
    "greeter_cooldown_seconds": (_GR, "number", "Greeting cooldown", "Minimum time between any two greetings, to survive raids.", {"unit": "seconds"}),
    "greeter_ignore_users": (_GR, "list", "Ignored users", "Usernames the greeter never welcomes, such as bots."),
    "twitch_client_id": (_TY, "text", "Twitch client ID", "Client ID of the Twitch app.", {"needed": True}),
    "twitch_client_secret": (_TY, "secret", "Twitch client secret", "Client secret of the Twitch app.", {"needed": True}),
    "twitch_user_access_token": (_TY, "secret", "Twitch user token", "Twitch user access token for channel queries."),
    "twitch_channel": (_TY, "text", "Twitch channel", "Twitch channel name, used for live checks and the embed.", {"needed": True}),
    "youtube_channel_id": (_TY, "text", "YouTube channel ID", "YouTube channel checked for a live broadcast."),
    "youtube_video_ids": (_TY, "list", "Homepage videos", "YouTube video IDs shown on the homepage while offline (the part after v= in a link, or the whole link)."),
    "cloudflare_tunnel_domain": (_LS, "text", "Tunnel domain", "Cloudflare tunnel hostname that reaches this backend."),
    "site_out_dir": (_LS, "text", "Site folder", "Absolute path of the built site's out/ folder that the backend serves.", {"needed": True}),
    "app_db_path": (_LS, "text", "Database path", "Where the SQLite database lives; empty uses the default."),
    "discord_live_enabled": (_DL, "bool", "Go-live post", "Post to Discord when the stream goes live."),
    "discord_webhook_url": (_DL, "secret", "Webhook URL", "Discord webhook the go-live post is sent to.", {"needed": "discord_live_enabled"}),
    "discord_live_role_mention": (_DL, "text", "Role mention", "Role or @everyone mention put in front of the post."),
    "discord_live_message": (_DL, "text", "Message", "Post text; {mention}, {name} and {title} are filled in."),
    "discord_live_poll_seconds": (_DL, "number", "Poll interval", "How often Twitch is checked for a live stream.", {"unit": "seconds", "min": 15}),
    "discord_live_cooldown_minutes": (_DL, "number", "Cooldown", "Minimum time between two go-live posts.", {"unit": "minutes"}),
    "streamerbot_golive_events": (_DL, "list", "Go-live events", "Streamer.bot event names that count as going live."),
    "discord_live_image_url": (_DL, "text", "Image URL", "Image attached to the post instead of the stream thumbnail."),
    "discord_live_image_file": (_DL, "text", "Image file", "Local image file attached to the post; wins over the URL."),
    "discord_live_youtube_wait_seconds": (_DL, "number", "YouTube wait", "How long to wait for a YouTube broadcast before posting.", {"unit": "seconds", "min": 0}),
}
FIELDS = {
    k: {"label": r[2], "description": r[3], "section": r[0], "type": r[1], **(r[4] if len(r) > 4 else {})}
    for k, r in _ROWS.items()
}


def _name_looks_secret(key: str) -> bool:
    return bool(_SECRET_RE.search(key))


def is_secret(key: str, data: dict) -> bool:
    if key in FIELDS:
        return FIELDS[key]["type"] == "secret"
    custom = (data.get("_custom_fields") or {}).get(key)
    if isinstance(custom, dict) and "type" in custom:
        return custom["type"] == "secret" or _name_looks_secret(key)
    return _name_looks_secret(key)


def masked(data: dict) -> dict:
    return {
        k: (SECRET_MASK if v else "") if is_secret(k, data) else v
        for k, v in data.items()
    }


def _infer(value, secret: bool) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "table" if all(isinstance(v, (str, int, float, bool)) for v in value.values()) else "json"
    return "secret" if secret else "text"


def with_needed(data: dict) -> dict:
    """data plus every "needed" key it lacks, empty, so the settings page can show and fill it."""
    return {**{k: [] if f["type"] == "list" else "" for k, f in FIELDS.items() if "needed" in f}, **data}


def schema_for(data: dict) -> dict:
    custom = data.get("_custom_fields") or {}
    fields = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if key in FIELDS:
            fields[key] = FIELDS[key]
        elif isinstance(custom.get(key), dict):
            fields[key] = {"label": key, "description": "", "section": custom[key].get("section", "Other"),
                           "type": custom[key].get("type", "text")}
        else:
            fields[key] = {"label": key, "description": "", "section": "Other",
                           "type": _infer(value, is_secret(key, data))}
    return {"sections": SECTIONS, "fields": fields}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_type(spec: dict, value) -> "str | None":
    t = spec["type"]
    unit = f" {spec['unit']}" if spec.get("unit") else ""
    if t == "number":
        if not _is_num(value):
            return "Must be a number"
        if not math.isfinite(value):
            return "Must be a finite number"
        if spec.get("integer") and value != int(value):
            return "Must be a whole number"
        if "min" in spec and value < spec["min"]:
            return f"Must be {spec['min']}{unit} or more"
        if "max" in spec and value > spec["max"]:
            return f"Must be {spec['max']}{unit} or less"
    elif t == "bool":
        if not isinstance(value, bool):
            return "Must be true or false"
    elif t == "list":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            return "Must be a list of text values"
    elif t == "table":
        if not isinstance(value, dict) or not all(isinstance(v, (str, int, float, bool)) for v in value.values()):
            return "Must be a table of simple values"
    elif t in ("text", "secret"):
        if not isinstance(value, str):
            return "Must be text"
    return None


def validate_update(updates: dict, data: dict) -> "tuple[str, str] | None":
    custom_now = dict(data.get("_custom_fields") or {})
    new_custom = updates.get("_custom_fields")
    if "_custom_fields" in updates:
        if not isinstance(new_custom, dict):
            return ("_custom_fields", "Must be an object")
        for name, spec in new_custom.items():
            if (not isinstance(spec, dict) or spec.get("type") not in FIELD_TYPES
                    or spec.get("section") not in SECTIONS):
                return ("_custom_fields", f"{name}: needs a valid type and section")
        custom_now.update(new_custom)
    for key, value in updates.items():
        if key == "_custom_fields":
            continue
        if key.startswith("_"):
            return (key, "Keys starting with an underscore are reserved")
        if key not in data and key not in FIELDS and not _KEY_RE.match(key):
            return (key, "Key must be lowercase letters, digits and underscores")
        spec = FIELDS.get(key) or custom_now.get(key)
        if spec is None:
            spec = {"type": _infer(data[key], False)} if key in data else None
        if spec is not None:
            err = _check_type(spec, value)
            if err:
                return (key, err)
    return None
