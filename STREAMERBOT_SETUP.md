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
| Authentication | **on** | Required for chat replies — see below. |
| Password | a long random string | This goes into `config.json` as `streamerbot_ws_password`. |
| Enforce | **on** | Requires authentication before *any* request, not just the privileged ones. |

Then click **Start**. The status indicator should read that the server is listening.

### Why authentication is on rather than off

Streamer.bot marks `SendMessage` as **Authentication Required**, and it is the only request
that carries that label. Chat replies — the roulette's announcements, and the `@user <reason>`
lines when a vote is refused — are all `SendMessage`, so authentication is not merely a
hardening step here; without it the replies are the feature most likely to stop working.

**Enforce** extends the requirement to every request, including `Subscribe`. That is what
makes the password actually protect the port: with Enforce off, an unauthenticated client can
still subscribe and read every message in your chat. Turn it on.

The backend implements the handshake (`streamerbot_client._authentication_hash`), which is the
same challenge-response scheme obs-websocket uses: Streamer.bot's `Hello` carries a per-connection
`salt` and `challenge`, and the answer is
`base64(sha256(base64(sha256(password + salt)) + challenge))`. The challenge changes every
connection, so an answer captured off the wire cannot be replayed onto a later one.

Put the same password in `config.json` on the Mac Mini:

```json
"streamerbot_ws_password": "the-long-random-string-you-set"
```

`config.json` is gitignored and must stay that way — this password grants the ability to read
your chat and post as your bot account.

## 3. Let the Mac Mini through the Windows firewall

Binding to `0.0.0.0` is not enough on its own; Windows Defender Firewall will still drop the
inbound connection. Add an inbound rule for TCP port 8080, and scope it as narrowly as the
setup allows:

- Restrict it to the **Private** network profile only, never Public.
- Under the rule's **Scope** tab, set the remote address to the Mac Mini's IP specifically,
  rather than leaving it open to any address.

The password is the real control now, so this is defence in depth rather than the only line —
but it is worth the two minutes. An exposed port is still something to probe and still
something that can be knocked over, whether or not a password guards what is behind it.

## 4. Point the backend at the gaming PC

Find the gaming PC's LAN address (`ipconfig` in a Command Prompt, the IPv4 Address under the
active adapter — typically `192.168.x.x`), then edit `config.json` on the Mac Mini:

```json
"streamerbot_ws_url": "ws://192.168.1.42:8080/"
```

(Along with `streamerbot_ws_password` from step 2.)

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
Answering Streamer.bot's authentication challenge
Authenticated with Streamer.bot
Subscribing to Streamer.bot events: {'Twitch': ['ChatMessage'], 'YouTube': ['Message']}
Streamer.bot accepted the event subscription: ...
```

That order is not cosmetic. With Enforce on, a `Subscribe` sent before `Authenticate` is
rejected, so the backend waits for the `Hello`, answers the challenge, and only subscribes once
the answer is accepted.

A socket that opens but whose subscription is never accepted delivers **zero** events while
looking completely healthy — an open connection and a quiet chat are indistinguishable from
the outside. That is why the subscription is tracked separately and reported: the admin
dashboard's status panel shows Streamer.bot as connected *and* warns
"Connected, but no event subscription - no chat command can fire" when only the first half
is true. `/api/status` exposes all three as `streamerbot_connected`, `streamerbot_authenticated`
and `streamerbot_subscribed`. `streamerbot_authenticated` is `null` when the server never issued
a challenge, so an unauthenticated setup is not flagged as a failure — only a refused answer is.

Then type `!roulette` in chat. The roulette overlay should open in OBS, and the bot account
should announce the session in chat.

## When something is wrong

Each failure has its own log line and its own dashboard warning, because they fail in
different places and the symptom — a quiet chat — is identical for all of them.

| Log line | What it means |
| --- | --- |
| `Connecting to Streamer.bot at ...` repeating | Nothing is listening at that address. Wrong `streamerbot_ws_url`, server not started, or the firewall rule is missing. |
| `Streamer.bot REJECTED authentication` | `streamerbot_ws_password` does not match the server's password. |
| `Streamer.bot asked us to authenticate but streamerbot_ws_password is empty` | The password was never put in `config.json`. |
| `Streamer.bot REJECTED the event subscription` | With Enforce on, this normally follows a failed authentication — fix that first. |
| `Streamer.bot rejected request send-N` | A `SendMessage` was refused after everything else succeeded. |

The dashboard shows the same three states without reading logs: connected, subscribed, and
authenticated. A failed authentication reads "Authentication refused - check
streamerbot_ws_password."

The backend subscribes even when authentication fails, deliberately: with Enforce **off** a
wrong password still leaves chat readable, and reading chat is most of what the roulette needs.
That is a degraded mode, not a working one — the replies will be refused.

To turn replies off on purpose, set `"roulette_chat_replies_enabled": false` in `config.json`.
The roulette still runs and the overlay still updates; only the viewer-facing announcements and
refusal messages stop.
