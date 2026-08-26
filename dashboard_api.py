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

import points
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

async def get_status(request: web.Request) -> web.Response:
    return web.json_response({
        "streamerbot_connected": streamerbot.is_connected,
        "widget_connections": {
            "total": widget_hub.connected_count(),
            "roulette": widget_hub.connected_count("roulette"),
            "badge": widget_hub.connected_count("badge"),
            "spotify": widget_hub.connected_count("spotify"),
        },
        # Placeholders - become real once their own tasks are built:
        "obs_websocket_connected": None,   # Task #8 (OCR) / Task #13 (Medal sync) territory
        "ocr_loop_running": None,          # Task #8
        "cloudflare_tunnel_up": None,      # not something this backend can check on itself
    })


def register_routes(app: web.Application):
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", update_config)
    app.router.add_get("/api/points/{username}", get_points_balance)
    app.router.add_post("/api/points/grant", grant_points_route)
    app.router.add_get("/api/logs", get_logs)
    app.router.add_get("/api/status", get_status)
