"""
Admin dashboard API routes, per Task #4's full feature list (Section 14):
  1. Config editor          - GET/PUT /api/config
  2. Points view/grant tool - GET /api/points/{username}, POST /api/points/grant
  3. Log viewer             - GET /api/logs
  4. Status/health panel    - GET /api/status

All protected by auth.auth_middleware except where explicitly left open
(health check, widget websocket, the login flow itself).
"""
from aiohttp import web

import credit_ocr
import health_checks
import ocr_agent
import points
import roulette
from config import config
from logger import LOG_FILE, get_logger
from streamerbot_client import streamerbot
from widget_hub import widget_hub

log = get_logger("DashboardAPI")


# ---------- Config editor ----------

async def get_config(request: web.Request) -> web.Response:
    return web.json_response(config.all())


async def update_config(request: web.Request) -> web.Response:
    """
    Accepts a JSON object of {key: value} pairs to update. Writes straight
    to the SAME in-memory config object the bot reads live (Section 14's
    "changes take effect immediately without a restart" requirement) - no
    restart needed, the next time any code reads config.get(...) it sees
    the new value.
    """
    try:
        updates = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    if not isinstance(updates, dict):
        return web.json_response({"error": "Body must be a JSON object of key/value pairs"}, status=400)

    for key, value in updates.items():
        config.set(key, value)
    config.save()
    log.info(f"Config updated: {list(updates.keys())}")
    return web.json_response({"status": "ok", "updated_keys": list(updates.keys())})


# ---------- Points testing tool ----------

async def get_points_balance(request: web.Request) -> web.Response:
    username = request.match_info["username"]
    try:
        balance = await points.get_user_points(username)
        return web.json_response({"username": username, "points": balance})
    except Exception as e:
        log.warning(f"Points balance lookup failed for {username}: {e}")
        return web.json_response({"error": str(e)}, status=502)


async def grant_points_route(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        username = body["username"]
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Body must be {\"username\": str, \"amount\": int}"}, status=400)

    try:
        new_total = await points.grant_points(username, amount)
        return web.json_response({"username": username, "granted": amount, "new_balance": new_total})
    except Exception as e:
        log.warning(f"Points grant failed for {username}: {e}")
        return web.json_response({"error": str(e)}, status=502)


# ---------- Log viewer ----------

async def get_logs(request: web.Request) -> web.Response:
    """Tail the last N lines of the backend log - default 200, capped at 2000."""
    try:
        n = min(int(request.query.get("lines", 200)), 2000)
    except ValueError:
        n = 200

    if not LOG_FILE.exists():
        return web.json_response({"lines": []})

    with open(LOG_FILE, "r") as f:
        all_lines = f.readlines()
    tail = [line.rstrip("\n") for line in all_lines[-n:]]
    return web.json_response({"lines": tail})


# ---------- Status / health panel ----------

def _credit_prediction() -> dict:
    """
    The same number the roulette's affordability filter will use for the
    next session, surfaced so it can be tracked from the dashboard rather
    than only being visible on the overlay mid-round.

    Deliberately calls roulette.affordable_weapons() rather than filtering
    a copy of the creds table here, so the dashboard can never disagree
    with what the roulette will actually do - including every fail-open
    path (no prediction yet, the filter switched off in config, or a creds
    table that would leave nothing votable), all of which come back as the
    full roster here exactly as they do there.
    """
    predicted = credit_ocr.get_predicted_credits()
    votable = roulette.affordable_weapons(predicted)
    return {
        "predicted_credits": predicted,
        "readings": credit_ocr.recent_readings(),
        "filter_enabled": config.get("roulette_affordability_filter_enabled", True),
        "votable_count": len(votable),
        "total_weapons": len(roulette.WEAPONS),
        "votable_weapons": votable,
        "weapon_creds_costs": {w: roulette.creds_cost_for(w) for w in votable},
    }


async def get_status(request: web.Request) -> web.Response:
    return web.json_response({
        "streamerbot_connected": streamerbot.is_connected,
        # Reported separately from the connection above on purpose: an open
        # socket with no accepted event subscription delivers nothing, and
        # looks exactly like a quiet chat. See streamerbot_client's docstring.
        "streamerbot_subscribed": streamerbot.is_subscribed,
        # null when Streamer.bot's Authentication toggle is off and it never
        # issued a challenge; false only when one was answered and refused,
        # which is the case worth flagging - SendMessage is the request
        # Streamer.bot requires authentication for, so chat replies die.
        "streamerbot_authenticated": streamerbot.is_authenticated,
        "credit_prediction": _credit_prediction(),
        "widget_connections": {
            "total": widget_hub.connected_count(),
            "roulette": widget_hub.connected_count("roulette"),
            "badge": widget_hub.connected_count("badge"),
            "spotify": widget_hub.connected_count("spotify"),
        },
        # Whether the gaming PC's agent is switched on, from its own
        # heartbeat rather than from whether captures happen to be
        # arriving - see ocr_agent's module docstring for why those are
        # not the same question. Tesseract's availability rides along
        # because a missing binary turns every capture into a 503, which
        # from the gaming PC looks exactly like a badly aimed region.
        "ocr_agent": {**ocr_agent.status(), "tesseract_available": credit_ocr.tesseract_available()},
        # Whether the public hostname still reaches THIS process. Not
        # introspection like everything above it: the answer is a real
        # round trip out through Cloudflare and back, run on its own timer
        # so opening this panel never waits on the network.
        "public_url": health_checks.status(),
        # Still a placeholder - OBS runs on the gaming PC and nothing here
        # connects to it yet (Task #13's territory).
        "obs_websocket_connected": None,
    })


def register_routes(app: web.Application):
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", update_config)
    app.router.add_get("/api/points/{username}", get_points_balance)
    app.router.add_post("/api/points/grant", grant_points_route)
    app.router.add_get("/api/logs", get_logs)
    app.router.add_get("/api/status", get_status)
