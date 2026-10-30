# DualBladeX stream backend

The always-on Python (aiohttp) backend on the Mac Mini: chat commands and overlays for the stream,
the admin dashboard's API, the public site, and the editor app's data, Drive and Discord layer.
It runs as one asyncio process started by `main.py`.

## Layout

```
main.py          entry point (launchd runs this)
core/            config, settings schema, SQLite, login/roles, HTTP server, site serving,
                 dashboard + public APIs, health check, the scheduler
stream/          the stream itself: Streamer.bot client, overlay sockets, roulette, credit OCR,
                 points (Cloudbot), Spotify requests, !rank, greeter, break screen,
                 Discord go-live, Twitch, Streamlabs
editor/          the editor app: projects/tapes/versions, comments, Google Drive, deletes,
                 upload recovery, backups, the gaming-PC uploader API, Discord DMs and reminders
tests/           pytest suite (one file per module)
docs/            setup guides: Streamer.bot, Google Drive, Discord bot
deploy/          the launchd LaunchAgent file
config.example.json   template for config.json (secrets; never committed)
```

`config.json`, `app.db` (+ `-wal`/`-shm`), `logs/`, `seen_chatters.json` and
`live_announce_state.json` live in this folder on the Mac Mini and are never committed.

## Run and test

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt -r requirements-dev.txt
cp config.example.json config.json      # first install only, then fill it in from the dashboard
venv/bin/python3 main.py                # run in the foreground
curl http://localhost:8765/health
venv/bin/python3 -m pytest tests -q     # safe on the Mac Mini: tests never touch config.json
```

## Deploy (Mac Mini)

```bash
cd ~/Documents/Stream && git pull
launchctl unload ~/Library/LaunchAgents/com.dualbladex.backend.plist
launchctl load ~/Library/LaunchAgents/com.dualbladex.backend.plist
curl -s http://localhost:8765/health
```

### Install or reinstall the launchd job

Needed on first install and whenever `deploy/com.dualbladex.backend.plist` changes (a `git pull`
alone does not update the installed copy). The `sed` fills in your macOS user name:

```bash
cd ~/Documents/Stream && git pull
launchctl unload ~/Library/LaunchAgents/com.dualbladex.backend.plist
sed "s/YOUR_MAC_USER/$(whoami)/g" deploy/com.dualbladex.backend.plist > ~/Library/LaunchAgents/com.dualbladex.backend.plist
plutil -lint ~/Library/LaunchAgents/com.dualbladex.backend.plist
launchctl load ~/Library/LaunchAgents/com.dualbladex.backend.plist
curl -s http://localhost:8765/health
rm -f /tmp/mac_mini_backend.out.log     # stdout now goes to /dev/null; the old file is no longer written
```

On first install `launchctl unload` reports that nothing is loaded; that is harmless.
It is a LaunchAgent, so it starts at login; power-cut recovery needs macOS automatic login and
`sudo pmset autorestart 1`. Crash logs: `/tmp/mac_mini_backend.err.log`.
The public hostname is a Cloudflare tunnel (`cloudflared`, installed as a LaunchDaemon) to port 8765.
