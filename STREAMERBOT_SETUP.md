# Streamer.bot setup (gaming PC)

Streamer.bot is a third-party Windows application. It is not part of this repository and
nothing here installs it — it has to be installed and configured by hand on the gaming PC,
once, before any chat command can work.

## Why it exists in this stack

The Mac Mini backend has no connection to Twitch or YouTube chat of its own. It never
authenticates with either platform and holds no chat credentials. Streamer.bot owns those
accounts, receives chat, and relays each message over its own WebSocket server; the backend
connects to that server as a client and reacts to what arrives. Chat replies travel the same
way in reverse, as `SendMessage` requests that Streamer.bot posts on the backend's behalf.

So the chain for `!roulette` is:

```
viewer types !roulette in Twitch chat
  -> Streamer.bot (gaming PC) receives the ChatMessage event
  -> WebSocket -> backend (Mac Mini), roulette.handle_chat_command()
  -> widget broadcast to OBS, and a SendMessage reply back through Streamer.bot
```

**The Mac Mini dials out to the gaming PC**, not the other way round. That direction is the
single most important thing to get right below, because the shipped default in `config.json`
is `ws://localhost:8080/`, which is only correct if both were the same machine — and they
are not.

## 1. Install and connect the platform accounts

1. Download Streamer.bot from https://streamer.bot and install it on the gaming PC.
2. Open **Platforms -> Twitch -> Accounts** and connect two accounts:
   - the **broadcaster** account, which is what lets Streamer.bot *read* chat;
   - the **bot** account, which is what posts replies. This can be the broadcaster account
     itself if there is no separate bot account, but the backend sends `"bot": true` on every
     reply, so a dedicated bot account is what it expects.
3. If YouTube is also being streamed to, connect it under **Platforms -> YouTube** as well.
   The backend subscribes to YouTube's `Message` event alongside Twitch's `ChatMessage`; it
   costs nothing to leave subscribed if YouTube is unused.

No Streamer.bot *actions* need to be created. The backend does not trigger any — it only
listens for chat events and sends `SendMessage` requests. Everything else lives in Python.

## 2. Enable the WebSocket server

Open **Servers/Clients -> WebSocket Server** and set:

| Setting | Value | Why |
| --- | --- | --- |
| Address | `0.0.0.0` | The default binds to loopback only, which the Mac Mini cannot reach. `0.0.0.0` listens on the LAN interface too. |
| Port | `8080` | Matches the default in `config.example.json`. Any port works as long as both sides agree. |
| Endpoint | `/` | The backend's URL ends in `/`. |
| Auto Start | on | Otherwise the server is down after every reboot and the backend just retries forever. |
| Authentication | **off** | The backend's client does not implement Streamer.bot's authentication handshake. See the note at the end. |

Then click **Start**. The status indicator should read that the server is listening.

## 3. Let the Mac Mini through the Windows firewall

Binding to `0.0.0.0` is not enough on its own; Windows Defender Firewall will still drop the
inbound connection. Add an inbound rule for TCP port 8080, and scope it as narrowly as the
setup allows:

- Restrict it to the **Private** network profile only, never Public.
- Under the rule's **Scope** tab, set the remote address to the Mac Mini's IP specifically,
  rather than leaving it open to any address.

This matters because the WebSocket server has authentication disabled: anything that can
reach port 8080 can send chat as the bot account and read every chat message. Keeping it
reachable only from the Mac Mini, on the home network only, is what makes that acceptable.

## 4. Point the backend at the gaming PC

Find the gaming PC's LAN address (`ipconfig` in a Command Prompt, the IPv4 Address under the
active adapter — typically `192.168.x.x`), then edit `config.json` on the Mac Mini:

```json
"streamerbot_ws_url": "ws://192.168.1.42:8080/"
```

Restart the backend afterwards:

```bash
launchctl unload ~/Library/LaunchAgents/com.dualbladex.backend.plist
launchctl load   ~/Library/LaunchAgents/com.dualbladex.backend.plist
```

DHCP will eventually hand the gaming PC a different address and silently break this. Reserve
its IP in the router's DHCP settings, or give the machine a static address.

## 5. Confirm it actually works

Two things have to be true, and only one of them is obvious. Watch
`/tmp/mac_mini_backend.out.log` after a restart:

```
Connected to Streamer.bot
Subscribing to Streamer.bot events: {'Twitch': ['ChatMessage'], 'YouTube': ['Message']}
Streamer.bot accepted the event subscription: ...
```

A socket that opens but whose subscription is never accepted delivers **zero** events while
looking completely healthy — an open connection and a quiet chat are indistinguishable from
the outside. That is why the subscription is tracked separately and reported: the admin
dashboard's status panel shows Streamer.bot as connected *and* warns
"Connected, but no event subscription - no chat command can fire" when only the first half
is true. `/api/status` exposes both as `streamerbot_connected` and `streamerbot_subscribed`.

Then type `!roulette` in chat. The roulette overlay should open in OBS, and the bot account
should announce the session in chat.

## A note on authentication and chat replies

Streamer.bot documents `SendMessage` as requiring authentication on its WebSocket server. In
practice, with the server's authentication toggle off, requests are accepted as sent. If
replies do come back rejected, the backend logs each one:

```
Streamer.bot rejected request send-N: {...}
```

Set `"roulette_chat_replies_enabled": false` in `config.json` to silence replies wholesale
rather than log a rejection per command. Reading chat and running the roulette continue to
work with replies off; only the viewer-facing announcements and refusal messages stop.
