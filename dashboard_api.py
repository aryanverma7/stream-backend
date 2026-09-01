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
import game_events
import health_checks
import ocr_agent
import points
import roulette
from config import config
from logger import LOG_FILE, get_logger
from streamerbot_client import streamerbot
from widget_hub import widget_hub

log = get_logger("DashboardAPI")

# What this backend answers when an upstream it depends on (Streamlabs,
# mostly) fails. NOT 502, which is what it used to be, and not any other
# 5xx: this process sits behind a Cloudflare tunnel, and a 5xx from here
# does not reach the browser as written - the edge serves its own branded
# "Bad gateway" HTML page in its place. Confirmed against a real failure,
# where the backend logged
#
#     Points balance lookup failed for pinkuthagoat: 404 ... User not found
#
# at 11:44:52 local and the browser was handed a Cloudflare 502 page whose
# Ray timestamp was 06:14:52 UTC - the same second. The panel then reported
# "JSON.parse: unexpected character at line 1 column 1", because the body
# it got was HTML, and the actual reason - a username Streamlabs does not
# have - was visible only in this log.
#
# 424 Failed Dependency says exactly the right thing (this request failed
# because a request it depended on failed) and, being a 4xx, is passed
# through untouched by every CDN. The frontend renders body.error for any
# non-ok status, so nothing there needed to change.
UPSTREAM_FAILED_STATUS = 424


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
        return web.json_response({"error": str(e)}, status=UPSTREAM_FAILED_STATUS)


async def grant_points_route(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        username = body["username"]
        amount = int(body["amount"])
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Body must be {\"username\": str, \"amount\": int}"}, status=400)

    try:
        new_total = await points.grant_points(username, amount)
        # new_balance is null when the grant is confirmed but the total
        # isn't knowable - the cloudbot backend's normal case. The panel
        # renders that as "granted", never as a balance of zero.
        return web.json_response({"username": username, "granted": amount, "new_balance": new_total})
    except Exception as e:
        log.warning(f"Points grant failed for {username}: {e}")
        return web.json_response({"error": str(e)}, status=UPSTREAM_FAILED_STATUS)


# ---------- Agent selection ----------

async def get_agents(request: web.Request) -> web.Response:
    """
    Every agent with ability prices on file, plus whoever is selected.

    Serves the merged view - config's overrides on top of the built-in
    table - so the dashboard's dropdown lists exactly what the roster
    calculation will actually find.
    """
    names = {roulette.normalize_agent(name) for name in roulette.AGENT_KIT_CREDS_COSTS}
    names |= {
        roulette.normalize_agent(name)
        for name in (config.get("roulette_agent_ability_costs", {}) or {})
    }
    return web.json_response({
        "current": roulette.current_agent(),
        "agents": [
            {"name": name, "kit_cost": roulette.agent_kit_cost(name)}
            for name in sorted(n for n in names if n)
        ],
    })


async def set_agent_route(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        name = body["agent"]
    except (KeyError, ValueError, TypeError):
        return web.json_response({"error": "Body must be {\"agent\": str}"}, status=400)

    stored, cost = roulette.set_agent(name)
    # kit_cost null means no prices on file - accepted deliberately, since
    # the name alone still shows up on the panel and in the log line that
    # says the fallback is in use, which is how a missing entry gets seen.
    return web.json_response({"agent": stored, "kit_cost": cost})


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
    agent = roulette.current_agent()
    return {
        "predicted_credits": predicted,
        # What is actually left for a gun once shields and abilities are
        # taken out, which is the number the roster was built from. The
        # raw reading is what the streamer sees in game; showing only that
        # makes the roster look wrong.
        "spendable_credits": roulette.spendable_creds(predicted),
        "reserved_credits": None if predicted is None else roulette.reserved_creds(predicted),
        # Which kind of round the roster was built for. Worth its own field
        # because it changes the roster in two ways at once - a smaller
        # reserve, and the sidearms staying on the wheel - and without it a
        # pistol-round roster looks like the filter misbehaving.
        "pistol_round": roulette.is_pistol_round(predicted),
        "agent": agent,
        # None means this agent has no ability prices on file and the flat
        # fallback is in use - worth surfacing, since that is the state
        # somebody has to fix by adding an entry.
        "agent_kit_cost": roulette.agent_kit_cost(agent or ""),
        "readings": credit_ocr.recent_readings(),
        # Not part of the prediction and never used as one - the last value
        # OCR ever managed to read, which survives the per-buy-phase reset
        # the window doesn't. Without it an empty window looks exactly like
        # an OCR pipeline that has never once worked, and those two are the
        # difference between "wait for the next round" and "go fix the
        # gaming PC". See credit_ocr.last_reading().
        "last_reading": credit_ocr.last_reading(),
        "filter_enabled": config.get("roulette_affordability_filter_enabled", True),
        "votable_count": len(votable),
        "total_weapons": len(roulette.WEAPONS),
        "votable_weapons": votable,
        "weapon_creds_costs": {w: roulette.creds_cost_for(w) for w in votable},
    }


async def get_status(request: web.Request) -> web.Response:
    # no-store, because the admin panel now polls this on a timer rather
    # than only when someone clicks Refresh. Nothing on this response
    # carries a validator, so without an explicit directive a browser or an
    # intermediary is free to apply its own heuristic freshness and hand
    # back a cached body - which on a status panel is not a stale page, it
    # is a wrong answer about whether the stream is ready.
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
        # What the wheel is doing, and what it last landed on. The last
        # result outlives its session on purpose: the question "which gun
        # am I being forced into" gets asked during the buy phase, by
        # which point the overlay has finished its spin - and answering it
        # meant opening the stream on another screen to watch a widget.
        "roulette": roulette.status(),
        # Live game state from the Overwolf app. Reported next to the OCR
        # numbers rather than instead of them, deliberately: this is the
        # pipeline that could replace credit_ocr entirely, and the two run
        # side by side until one has earned that. Its `money` is the local
        # player's CURRENT credits, which is not the same number as the
        # prediction above - see game_events' module docstring.
        "game_events": game_events.status(),
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
    }, headers={"Cache-Control": "no-store"})


def register_routes(app: web.Application):
    app.router.add_get("/api/config", get_config)
    app.router.add_put("/api/config", update_config)
    app.router.add_get("/api/points/{username}", get_points_balance)
    app.router.add_post("/api/points/grant", grant_points_route)
    app.router.add_get("/api/agents", get_agents)
    app.router.add_post("/api/agents", set_agent_route)
    app.router.add_get("/api/logs", get_logs)
    app.router.add_get("/api/status", get_status)
