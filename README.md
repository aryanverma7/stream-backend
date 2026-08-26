# Mac Mini Backend — Task #3 Skeleton

This is the always-on backend that runs on the Mac Mini. Per the project notes
(Section 3), this task builds the **skeleton only** — the HTTP server, the
WebSocket server for "smart" widgets, the outbound connection to Streamer.bot,
config loading, and logging. Feature logic (Roulette, Spotify, OCR, clips)
gets wired into this skeleton in their own later tasks, not here.

## What this does right now

- Runs an HTTP server with a `/health` check
- Runs a WebSocket server at `/ws/widgets` that future widgets (Roulette, the
  Forced Buy badge, Spotify now-playing) will connect to for live updates
- Connects outbound to Streamer.bot and logs whatever chat/event messages
  arrive (real command handling comes in later tasks)
- Logs everything, tagged by subsystem, to `logs/backend.log`

## Setup

### 1. Python environment

```bash
cd mac_mini_backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Config

```bash
cp config.example.json config.json
```

Open `config.json` and fill in real values. Only `http_host`, `http_port`,
and `streamerbot_ws_url` actually matter for this task specifically — the
rest are placeholders for later tasks, left there so this stays one evolving
config file instead of needing a new one created every task.

### 3. Test run it manually first

```bash
python3 main.py
```

You should see startup logs, and (once Streamer.bot is running on the gaming
PC and reachable at the URL in your config) a "Connected to Streamer.bot"
line. In a separate terminal, confirm the HTTP server is alive:

```bash
curl http://localhost:8765/health
# should return: {"status": "ok"}
```

Stop it with Ctrl+C once you've confirmed it works before moving to the
always-on setup below.

### 4. Install as an always-on LaunchAgent

This is what actually makes it "always-on" — auto-starts, auto-restarts on
crash, per Section 3's reasoning for why a LaunchAgent matters over just
leaving a terminal window open.

1. Edit `com.dualbladex.backend.plist` — replace both instances of
   `/path/to/mac_mini_backend` with the actual full path to this folder on
   your Mac Mini (e.g. `/Users/yourname/mac_mini_backend`).
2. Copy it into place and load it:

```bash
cp com.dualbladex.backend.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.dualbladex.backend.plist
```

3. Confirm it's running:

```bash
curl http://localhost:8765/health
```

To stop/unload it later (e.g. while actively developing and wanting manual
control instead):

```bash
launchctl unload ~/Library/LaunchAgents/com.dualbladex.backend.plist
```

launchd-level crash logs (separate from `logs/backend.log`) land at
`/tmp/mac_mini_backend.out.log` and `.err.log` — check these first if the
LaunchAgent doesn't seem to start at all.

## Cloudflare Tunnel (needed for Task #5's OAuth redirect, not this task)

Not required to get this skeleton itself running, but since Task #5 needs an
HTTPS redirect URL, it's worth setting up now while you're already in
terminal setup mode:

1. Install `cloudflared` (`brew install cloudflared` on macOS)
2. `cloudflared tunnel login` — authenticates against your Cloudflare account
3. `cloudflared tunnel create <name>` — creates a named tunnel
4. `cloudflared tunnel route dns <name> <subdomain.yourdomain.com>` — points
   a subdomain at it
5. Create a small `config.yml` for cloudflared routing that subdomain to
   `http://localhost:8765` (or whatever port you're running this backend on)
6. `cloudflared tunnel run <name>` to test, then install it as its own
   LaunchAgent the same way as above once confirmed working, so the tunnel
   itself is also always-on

## Next steps

Task #4 (admin dashboard) will add real routes to this same server — the
config editor, the points test tool, the log viewer, and the status panel —
rather than being a separate service, per the architecture decision in
Section 14.
