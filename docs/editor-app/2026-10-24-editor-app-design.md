# Editor App — design

Date: 2026-10-24. Status: awaiting the streamer's approval.
Wireframes (layout only, not final visuals): https://claude.ai/artifact/XG2WnQFHr4QEr685HawViy

## Intent

DualBladeX has hired a video editor who turns 3+ hour stream VODs into ~10 minute funny videos.
Today the handoff is manual: upload a 20GB+ VOD to Google Drive, share it, wait, delete it later;
review happens over chat with typed timestamps. This app removes the manual handoff and gives
both people one place to review, discuss and finish videos.

The overriding rule: **the streamer being away must not block the editor.** The gaming PC may be
off, the streamer may be outside. (Exception accepted knowingly: power cuts take the Mac Mini down,
and with it the editor pages — see "Power cuts".)

## Goals

1. Right-click a VOD on the gaming PC → it uploads to the streamer's Google Drive by itself, and the
   editor is told it is ready.
2. The editor downloads it; the VOD is then deleted from Drive (two-step confirmation + 1 hour undo).
3. The editor uploads each cut ("iteration") through the site straight into Drive.
4. The streamer reviews at full quality with timestamped and general comments, threaded replies,
   checkboxes carried into the next iteration, and finally downloads the approved video, after which
   that tape's files are deleted from Drive (same two-step + 1 hour undo).
5. Automatic technical checks on every iteration.

## Non-goals

- Publishing to YouTube (dropped: unaudited API projects upload locked-private; see conversation).
- Payment tracking (paid monthly, outside the app).
- Moving anything to the cloud. Everything runs on the Mac Mini. Revisit only if the streamer asks.
- Voice comments, quick-comment presets, Shorts markers, watch-together, weekly digests, chapter
  export, drawing on frames, deadlines, activity history — all considered and declined.
- Exact export targets for the checks — the streamer will supply them at build time (see Later).

## Anti-goals (would make it a failure even if it works)

- Any step that requires the streamer to remember to start a program or babysit a transfer.
- Large files passing through the Mac Mini (it has neither the disk nor the bandwidth).
- Anything deleted from Drive without an explicit two-step confirmation.
- A power cut silently losing or corrupting state, firing a delete early, or spamming Discord.
- Uploading during a live stream and eating the stream's upload bandwidth.

## Constraints

- Mac Mini (2012, macOS Catalina, low disk) runs the existing single-process aiohttp backend
  (`mac mini/Stream`) and serves the Next.js static export (`mac mini/Site`) via Cloudflare Tunnel at
  `hub.dualbladex.org`.
- Cloudflare free plan rejects request bodies over 100MB → no large uploads through the tunnel.
- The streamer has a paid Google One plan (size not stated; storage check required anyway).
- Frequent power cuts at the streamer's home; no UPS for now. Assume Mac Mini, gaming PC and router
  lose power together; the editor's power is unaffected.
- Existing project rules in `CLAUDE.md` apply: new routes and `auth.py` `open_paths`; optional
  fields on frontend status types; injected clocks for timing code; plain `<link>` fonts; etc.
- Gaming PC is Windows with Python already installed (the credit-OCR agent lives in `gaming pc/`).

## Approaches considered

**Hosting**
- *Mac Mini (chosen).* One machine, existing stack. Cost: editor pages are down during power cuts.
- *Cloudflare Workers + D1 for the editor side.* Survives power cuts, free, removes most edge cases;
  cost is a second backend in JavaScript and less integration. Declined for now; the streamer may
  revisit.
- *Whole stack on a rented server.* Doesn't help during power cuts (gaming PC is off then too, so no
  stream), costs monthly or a rewrite. Declined.

**Code placement**
- *A. Inside the existing backend and site (chosen).* Reuses auth, config, tunnel, launchd, status
  panel and deploy flow; one service to bring back after a power cut. Cost: shares a process with
  the stream stack.
- *B. Separate app on the Mac Mini.* Full isolation, but auth/config/launchd/deploy all doubled.
- *Off-the-shelf review tools (Frame.io etc.).* Tiny free storage, not Drive-backed. Ruled out.

**VOD transfer**
- *Direct PC→editor.* Requires the gaming PC to stay on; rejected by the streamer.
- *Google Drive (chosen).*

**Gaming PC uploader**
- *Browser upload.* Cannot resume after a power cut without re-selecting the file. Rejected.
- *Python uploader started from Explorer's right-click menu (chosen).*
- *Auto-prompt on every new OBS recording.* Declined; streamer prefers right-click.

## Data model

- **Project** = one VOD. Survives the VOD's deletion. Identified by stream date, stream title (from
  the backend's existing Twitch/Streamer.bot knowledge where available, else the filename) and
  original filename; renamable.
- **Tape** = one video cut from a project. Has a status label (see Statuses) and "tape notes" (free
  text: title ideas, thumbnail ideas).
- **Iteration** = one uploaded version of a tape (v1, v2, …). Holds the Drive file id, size, check
  results, a thumbnail frame, upload time.
- **Comment** = belongs to an iteration. Either timestamped (a point or a range `start–end`) or
  general (no timestamp). Has a type: **Fix / Keep / Question**. Has a thread of replies from either
  person. Has a resolved state.
- When a new iteration of a tape is uploaded, every **unresolved** comment from the previous
  iteration appears on the new one as a checkbox (carried over, with its thread). Resolved ones stay
  visible on their original iteration only. *(Builder's call: carry the comment itself forward rather
  than copying, so its thread is continuous.)*
- **Personal to-do list** per login, private to its owner. Plain items with done/undone.
- **Per-user read state** ("last seen" per item) for New badges and the notification bell.
- **Playback position** per user per iteration (resume where you stopped).

### Statuses
**Cutting / In review / Changes asked / Final.** Status is a label; views offer filter and
sort by status, date and name instead of fixed columns.

## Roles and login

- Existing GitHub OAuth (`auth.py`) gains roles: `authorized_github_usernames` stays the admin list;
  a new editor list grants access to `/editor` and its APIs only. The editor needs a GitHub account.
- The streamer (admin) can open everything, including the editor-side views.
- **Roles are enforced on the server, per route, not by hiding pages.** Today `auth_middleware`
  only asks "is there a session?", so any logged-in account can read and write `/api/config`
  (every secret, including the login allowlist). Before an editor account exists, every existing
  `/api/*` admin route must require the admin role, and the editor gets only the `/editor` APIs.
- Sessions must survive a backend restart (today they are in memory; a power cut would log both
  people out). Store them in the same SQLite database.

## Flows

### 1. VOD upload (gaming PC → Drive)
- One-time setup adds **"Send to editor"** to Windows Explorer's right-click menu for video files,
  and a logon task.
- Clicking it launches the uploader (windowless Python, `pythonw`) with a tray icon showing
  progress; it exits when done.
- Before uploading: check Drive free space; refuse with a clear message if the file will not fit.
- It registers the project with the backend (secret-gated like the OCR agent's routes, listed in
  `open_paths`), then uploads directly to Drive with Google's **resumable upload**, persisting the
  session URI and offset to disk.
- **Resume:** the logon task runs the uploader at Windows login; if an upload is unfinished it
  continues silently, otherwise it exits immediately. Google keeps a resumable session about a week;
  past that, restart the upload.
- **Pause while live:** the uploader asks the backend whether the stream is live (the backend already
  hears go-live from Streamer.bot and polls Twitch — `discord_live.py`; the builder must add a live
  *level*, not only the go-live edge) and pauses until it is offline.
- Prevent Windows sleep while uploading.
- When the upload completes: the file is shared with the editor's Google account, and the editor
  gets **one** Discord DM plus a popup next time he opens the site. Uploads that finish close together
  (several files sent at once) are **one grouped DM**, e.g. "8 new clips are ready" or "2 new VODs and 3 new
  clips are ready" with their names, sent once no further upload has finished for 10 minutes
  (streamer's rule, 2026-10-28).

### 2. VOD download and delete
- The editor downloads via Drive using his own Google account (works even while the Mac Mini is
  down).
- The site records when he clicks Download.
- He then does the **two-step delete** (see Deletion).
- If he has clicked Download but not confirmed the delete: starting 3 hours after the click, a
  Discord DM every hour from **12:00 to 22:00 (streamer's timezone, the Mac Mini clock)** until he
  confirms.

### 3. Iteration upload (editor browser → Drive)
- On the tape (drag-and-drop), the editor's browser uploads straight to Drive using a resumable
  session the backend creates; the Mac Mini only records metadata. The backend hands out a
  short-lived access token / session; if it expires mid-upload (e.g. Mac Mini down) the upload pauses
  and resumes when the Mac Mini is back.
- Files live in a known Drive folder per tape, so the backend can reconcile (see Power cuts).
- After upload: checks run (see Checks), a thumbnail is taken, the streamer gets a Discord DM.

### 4. Review
- The streamer opens an iteration. **The whole file is loaded into the browser before playback** (the
  streamer accepted a wait up front for guaranteed full quality, no stutter). Drive's own player is
  not used (it re-encodes).
- Comments are integrated with the player: press **C** or the button to pause and capture the
  current time; drag or set an end time for a range comment; or add a general comment.
- Comment markers on the scrub bar; clicking a comment jumps there; the comment list highlights the
  current comment as the video plays.
- Player keys: frame step, playback speed, the usual play/seek keys, and a `?` shortcut sheet.
- Compare versions: a key switches between this iteration and the previous one at the same
  timestamp. Low-priority extra.
- **Start watching now** (streamer, 2026-10-29, for slow connections): instead of waiting for the
  whole download, play at once. Downloaded parts play from disk, the rest streams from Drive at full
  quality, and the download continues in the background. A ring icon shows how much is downloaded.
- Unsent comments are kept in browser storage and sent when the backend is reachable again.
- Search across all comments and threads.

### 5. Threads and notifications
- Either person can reply on any comment; it is a thread.
- In-site: a notification bell and **New** badges (per-user last-seen vs. item last-changed).
- Discord DMs are **batched**: one DM after the other person has been quiet ~10 minutes
  ("Aryan left 6 comments on Jett dash v2"), not one per reply.
- DMs go to both people via a Discord bot over Discord's REST API (create DM channel, post message —
  no gateway connection needed). The bot must share a server with both.
- DM events: VOD ready (editor), delete-not-confirmed reminders (editor), new iteration (streamer),
  batched comment/reply digests (both), "site is back online" after an outage (editor).

### 6. Final and cleanup
- The streamer marks a tape **Final** (confirmation dialog), downloads it, and then the two-step
  delete schedules every iteration file of that tape for deletion. The tape record, comments and
  threads remain as history.

## Deletion rule (all deletes)

1. Two-step confirmation: Delete, then a deliberate second action (type the tape/project name).
2. One-hour grace with a visible countdown and **Undo**.
3. Delete is **permanent** in Drive (not Trash — Trash counts against storage for 30 days).
4. Deletes are idempotent: mark *deleting* → call Drive → mark *deleted*; a 404 from Drive counts as
   success; anything left in *deleting* at boot is retried.
5. After any backend restart, every pending delete gets **at least a full hour of undo again**
   (deadline = max(deadline, boot + 1h)), so an outage can never eat the undo window.

## Checks on every iteration

Run in the browser (the file is already loaded for playback / being uploaded) or via `ffprobe` on a
ranged read — **builder's call**; the Mac Mini must not download whole files. Pass / warn / fail
badges on the iteration card and review screen.
- Resolution, frame rate and video bitrate only (streamer, 2026-10-28: no loudness, peak or length
  checks). Duration is shown, not checked.
- Thresholds (streamer, 2026-10-28):
  - Resolution: tapes 1920x1080, reels 1080x1920 (vertical). Lower = warn.
  - Frame rate: 60 fps pass; 30 to under 60 warn; under 30 fail.
  - Video bitrate: at least 50 Mbps pass; under 50 fail.

## Pages and navigation

Navigation is the streamer's zoom idea plus a breadcrumb: All projects → (zoom) a project's tapes →
(zoom) a tape's iterations → review player. Breadcrumb (`All projects › Oct 21 VOD › Jett dash › v2`)
jumps any number of levels; Back/Esc zooms out one level. Both roles use the same space; available
actions differ.

- **Iteration card:** thumbnail, version, check badges, and below it a small summary card of that
  iteration's comments and checkbox states ("3 of 5 resolved"). Hover menu (tap on touch): Play,
  Change status, Edit checklist, Add comment, Download.
- **Editor Home:** what needs him now — VODs ready, deletes to confirm, tapes with open comments.
- **Fix list (editor):** every unresolved comment across tapes; click jumps to the moment.
- **Storage (streamer):** what is on Drive, sizes, Drive free space, scheduled deletions with Undo.
- **Transfers panel:** persistent corner panel with every upload/download in progress.
- **To-do:** personal list, reachable from any page.
- **Brand kit:** a link to one Drive folder shared with the editor (intro, outro, logos, fonts, SFX).
  Not used today; just the link and the share.
- Upload/download live where the thing lives (no separate upload page): New project on the gaming
  PC; VOD download on the project card; iteration upload on the tape; final download on the tape.

Visual design is **not** settled. The wireframes are layout only. A proper design pass in the site's
existing style comes during the build, including the zoom transitions.

## Power cuts

Mac Mini side:
- `pmset autorestart 1` (start after power failure) and macOS automatic login (requires FileVault
  off) — the backend is a LaunchAgent and only starts after login. `cloudflared` must also start on
  its own; verify how it is installed.
- **SQLite** (stdlib `sqlite3`, WAL, `synchronous=FULL`) for all app state. No JSON state files.
- **Times stored as absolute moments, never as running timers** (delete-at, next-reminder-at,
  send-digest-at). On boot, the scheduler processes anything due — subject to the rules below.
- **Clock sanity:** no scheduled job runs until the system clock is later than the last timestamp
  written before shutdown (a weak RTC battery can boot the clock into the past).
- **Network not up yet:** Drive/Discord calls retry with backoff; a failed attempt is never recorded
  as done.
- **Missed reminders:** after an outage, one catch-up DM (inside the 12–22 window), never a burst.
- **Reconciliation at boot:** compare each tape's Drive folder with the database — register uploads
  that finished while the Mac Mini was down, clean up abandoned partial uploads, retry pending deletes.
- **Nightly backup** of the SQLite file to Drive (small). Worst case after disk damage: lose a day.
- "Site is back online" DM to the editor after an outage.

Out of scope, noted: existing JSON state files in the stream backend (`config.json` writes, the
greeter's seen list, live-announce state) are not power-safe today; fix separately.

## Integration with the existing code

- New backend modules in `mac mini/Stream` (e.g. editor data, Drive client, scheduler, Discord DM),
  registered from `main.py`/`server.py` before `site_server.py`'s catch-all. Any secret-gated
  machine-to-machine route goes in `auth.py` `open_paths`.
- Google Drive OAuth: a one-time connect flow like `spotify_oauth.py`, refresh token saved
  atomically (Google may rotate it — same lesson as Spotify's).
- New `/editor` pages in `mac mini/Site`, data via `lib/use*.ts` hooks with tests, as the rest of the
  site does. Static export — no Next server.
- Admin dashboard: an Editor row on the Status panel (Drive connected, storage, Discord bot) is
  reasonable — builder's call.
- Gaming PC uploader lives in `gaming pc/` beside the agent, with its own tests and a setup script for
  the right-click entry and logon task.

## Decisions made after the builder check (author's calls — streamer to confirm)

- **Drive scope: `drive.file`** (app-created files only). Non-sensitive, so the OAuth app can be
  published to production without Google verification, and refresh tokens do not expire weekly as
  they do in Testing mode. Consequences: the Storage page lists only the app's own files plus Drive's
  overall free space; the app creates the Brand kit folder itself (nothing exists today).
- **Iteration uploads need no token in the browser:** the backend creates the resumable session
  (passing the site's `Origin` so the browser may use it) and hands the browser only the session
  URI, which authorises that one upload.
- **Carried-over comments and re-cut timing (streamer's rule):** on the next iteration a carried
  comment is a **checkbox with no timestamp on that iteration** — no scrub-bar marker, never pinned
  to a time in the new cut, because a re-cut moves everything. It shows its origin as text
  ("from v1 at 03:42"); clicking that opens the old iteration at that moment. Schema: a comment
  has only its origin iteration + time (or range); carry-over is a per-iteration resolved state.
- **Review devices: Chrome or Firefox on the gaming PC, and a ~3-year-old laptop's browser.** No
  phone/iPad/Safari support needed. To avoid holding 1–4GB in RAM on the laptop, the player
  downloads the file into the browser's private disk storage (OPFS, supported by both Chrome and
  Firefox) and plays from there. Downloaded versions stay while you are inside their project (VOD or clip), so reopening or comparing is instant; leaving the project deletes them, including an unfinished download. After a closed tab this happens on the next page load unless it opens in the same project, so a reload resumes (streamer, 2026-10-29; a browser extension and close-time deletion were considered and declined). The
  editor's Fix list uses the same player (he reviews from Drive the same way).
- **Exactly one editor**, as three config values: GitHub login, Google email (for Drive sharing),
  Discord user ID. Generalising to several editors is a later change.
- **Storage guard on iteration uploads too:** refuse with a clear message when Drive free space is
  short. Abandoned tapes and projects: the streamer can delete any tape or project from the Storage
  page under the normal deletion rule; nothing is deleted automatically.
- **"Live" for pausing uploads = live on Twitch**, checked by the Mac Mini against the Twitch API
  (cached 60 s; the streamer always streams to Twitch). Changed 2026-10-28 at the streamer's request:
  originally OBS via obs-websocket on the PC, dropped as extra setup for no gain. Uploads resume 5
  minutes after the stream ends. If Twitch or the Mac Mini cannot answer, upload.
- **The Discord bot is new setup:** a bot application, its token in `config.json`, invited to the
  streamer's server; both users' Discord IDs in config. `discord_live.py`'s webhook is unrelated.

## Also in this build: settings page and security fixes

Agreed with the streamer to ship alongside the editor app (wireframe board "Settings editor").

**Settings page** replaces the raw-JSON config editor in `/admin`:
- Sections in a sidebar (Streamer.bot, Points, Roulette, Credit OCR, Spotify, Rank, Greeter,
  Discord go-live, Twitch & YouTube, Break screen, Login & site, Editor app), each with a count.
- Search across every setting's name, description and key, with matches in other sections noted.
- Each setting: readable name, one-line description, its real key in small text, and an input
  matching its type — number with unit, text, on/off, list as removable chips with Add, object as a
  small key→value table.
- Validation before save (e.g. minimums); changed fields highlighted; a sticky bar with the
  unsaved-change and error count, Save (disabled while errors exist) and Discard.
- **Secrets are never sent to the browser**: shown as `••••` with Set / Not set and a Replace action;
  the backend keeps the stored value unless a new one is submitted.
- **Add setting**: key (new, lowercase with underscores), type (number, text, on/off, list, table,
  secret), value, section. Any key in `config.json` the page has no description for appears under
  **Other** automatically, so nothing is ever hidden.
- **Advanced (raw JSON)** remains as an escape hatch, with secrets still masked.
- Builder's call: where the per-key metadata (label, description, section, type, limits, secret
  flag) lives — one schema both backend and frontend read is preferred over two copies.

**Security fixes** (from the 2026-10-25 audit):
1. Role checks on every admin route (see Roles and login).
2. Secrets masked on `GET /api/config`; `PUT` treats the mask as "unchanged".
3. Session cookie gets `Secure`; sessions persist in SQLite with a 30-day expiry and a working
   logout.
4. `open_paths` matches whole path segments, not raw prefixes (`/clips` must not open `/clipsadmin`).
5. `config.json` is written atomically (temp file + rename).
6. Upgrade `aiohttp` 3.10.11 → current (3.14.5 had no advisories at audit time) and `Pillow`
   12.3.0 → 13.x; run the backend suite on the Mac Mini before deploying. `python-socketio` stays
   on 4.6.1 (Streamlabs protocol); its RCE advisory concerns server-side message queues, not the
   client used here.
7. Upgrade `next` 16.3.1 → ≥16.4.1 plus `npm audit fix` (sharp, source-map-js). Not exploitable as a
   static export, but cheap. Node at the build machines ≥ 24.22.1 (or 22.23.4 / 26.12.1).
8. Check `com.dualbladex.backend.plist` against the copy actually installed on the Mac Mini (the
   tree's copy has `/path/to/...` placeholders and the system Python), since auto-restart after
   power cuts depends on it.
9. Add an ESLint flat config so `npm run lint` works.

## Clips and reels (added 2026-10-27)

- An upload **shorter than 5 minutes** (Medal clips) is a **Clip**; anything longer is a VOD. The
  uploader reports the duration; the backend classifies (`duration < 300 s` → clip).
- The streamer often sends several clips at once; **each clip is its own item**, never grouped
  automatically. The editor works through them about one a day (UI lists clips oldest first).
- Each Clip automatically gets **exactly one Reel** (a tape of kind "reel") with unlimited versions,
  comments, carry-over, Final and deletes exactly like a tape. A second reel on a clip is refused.
  A redo is simply the next version.
- **Combine:** either user may combine 2+ clips into one longer reel: one set of versions, one thread,
  one checklist, one Final. Combining **replaces** those clips' own reels; each clip shows "used in"
  that reel. Allowed only while none of the involved reels has a version; **uncombine** likewise.
  **A clip is used in at most one reel** (streamer's default; no clip in two reels).
- Source clips are downloaded and deleted individually with the normal two-step + 1-hour undo.
  A clip project that hosts a combined reel cannot be project-deleted until that reel's files are
  deleted.
- **Start over** (streamer only): on a tape or reel, resolves every open comment on the latest
  version in one action, so the next version starts clean apart from new notes.
- Reel-specific technical checks (vertical 9:16, length): **on hold** by the streamer's choice.
- **Page hardening (Plan 5):** a Content-Security-Policy header allowing scripts only from the site
  and the CDN hosts the hero uses, `frame-ancestors 'none'`, and `nosniff`. Recommend both users open
  the dashboard in a browser profile with no extensions.

## Left to the builder

- Module boundaries and API shapes.
- Browser-side vs. ranged-`ffprobe` checks.
- Reading the Drive file into the browser for playback (auth'd range/whole-file fetch into a Blob or
  MediaSource) — must be tested early on real 1–4GB files; it is the riskiest technical piece.
- Exact keyboard bindings.
- Whether the comment-to-checkbox carry-over is the same row or a linked copy (preference: same row).

## Later (to-do list)

- Export specs: supplied 2026-10-28 (see "Checks on every iteration").
- Optional: move the editor side to Cloudflare if power cuts keep hurting the editor.
- Optional: UPS for the Mac Mini.
