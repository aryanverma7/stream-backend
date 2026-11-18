# Editor App Plan 6: Frame.io round

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap to Frame.io on free Drive storage. Six parts, each one PR (or one PR per repo), shipped in this order:

1. **6A** Roadmap and spec clean-up
2. **6B** Playability bar and marker export
3. **6C** Side-by-side compare
4. **6D** Comment attachments
5. **6E** Share links and guest notes
6. **6F** Hinglish auto transcript (**postponed 2026-10-31**: the streamer has no access to the gaming PC; build later, starting with Task 0)

The cheap, low-risk parts ship first. The two riskiest parts (6E share links, 6F transcripts) come last and each ships on its own.

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md`, section "Frame.io round (added 2026-10-31)". Earlier plans: 5A (`2026-10-28-editor-app-5a-site.md`) and 5B (`2026-10-29-editor-app-5b-player.md`). Reuse their shell, router, `editorApi`, `ReviewPlayer`, `useComments`, unsent queue, `resumableUpload`, `media-link`, service worker and `AskDialog`; don't rebuild them.

**Tech stack:**
- Backend: Python aiohttp in `mac mini/Stream` (`core/`, `editor/`).
- Site: Next.js 16 static export in `mac mini/Site`, with vitest, Testing Library and Playwright (`e2e/`).
- Gaming PC: Windows Python in `gaming pc/` (pc-ocr repo).

## Global constraints (all parts)

- Everything in `CLAUDE.md` holds. In particular:
  - New non-browser routes go in `OPEN_PATHS`.
  - Admin-only checks run inside the `/api/editor` handlers and answer `403 {"error": "Only the streamer can do this."}`.
  - `db.transaction()` is the only multi-statement write, and it is never held across an `await`.
  - Every DDL runs before the transaction opens.
  - Timing code takes `now`.
  - A new config key needs a `FIELDS` entry.
  - New `/api/status` and API fields are optional on the frontend.
- **Large files never pass through the Mac Mini.** That covers attachments, guest playback and transcript audio alike.
- **Secrets stay where they are today.** There are two documented exceptions, both in 6E:
  - The public, referrer-restricted Drive API key goes to the guest page. Google designs that key to be public.
  - The share token sits in a URL *fragment*, and the backend stores only its sha256.
- **Every fix and feature gets a test that fails without it.** After every site task, all of these must pass: the site suite, `node_modules/.bin/tsc --noEmit`, `npm run lint` (0 errors) and `npx next build`. The backend suite runs through uv as in `CLAUDE.md`.
- **Git hygiene:**
  - Stage files by name.
  - Commit with `-c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com` plus the session trailers.
  - Branch `editor-app-plan-6<letter>` in each repo touched.
  - Push only with the streamer's go-ahead. Never push to a PR after calling it ready; check `gh pr view N --json headRefOid` against local HEAD.
- **Deploy steps:** every part ends with a list of manual steps for the streamer: Mac Mini, site build, gaming PC, Google Cloud, OBS.
- **Look:** use the site's existing tokens.
  - Kind colours: Fix `rec`, Keep `signal`, Question `amber`.
  - `prefers-reduced-motion` turns off any new motion.
  - The Persona 5 restyle is still undecided, so build on main's look.

---

## 6A: Roadmap and spec clean-up (docs only)

**Files:** `docs/superpowers/plans/2026-10-25-editor-app-roadmap.md`, and the stale copy in `mac mini/Stream/docs/editor-app/` (spec, roadmap and this plan).

- [ ] Tick row "Clips list oldest first, used in, Combine/Uncombine UI, Start over button": all built (5A plus stream-site#9).
- [ ] CSP row: tick it, citing the spec note added 2026-10-31.
  - The homepage has no CSP, and `'unsafe-inline'` stays.
  - The extension-free-profile advice goes into `GDRIVE_SETUP.md` as one sentence (see 6A task 4).
- [ ] Iteration-cards row: keep it unticked. Add sub-rows for what is missing:
  - (a) the "Edit checklist" menu item opens the version on its comment panel, filtered to carried checkboxes;
  - (b) the summary shows as a small card under the thumbnail ("3 of 5 resolved", open Fix / Question counts), not inline text.
  - Both are built in 6B task 5.
- [ ] Add one line to `GDRIVE_SETUP.md`: "Open the dashboard and the editor in a browser profile with no extensions; an extension can read every page you open."
- [ ] Design-pass row: note "waiting on the Persona 5 decision (stream-site branch theme/persona5)".
- [ ] Plan 1's handover note: replace "the real setup is LaunchDaemons (backend and cloudflared)" with "backend is a LaunchAgent, cloudflared a LaunchDaemon (see CLAUDE.md)".
- [ ] Add a "Plan 6" section to the roadmap with one unticked row per part below. Under "Still to verify on real machines", replace "ProRes/HEVC/H.264 playback" with "H.265 playback on the laptop and on a guest phone".
- [ ] Copy the spec, the roadmap and this plan into `mac mini/Stream/docs/editor-app/`, then commit on `editor-app-plan-6a`.

**Deploy:** none.

---

## 6B: Playability bar and marker export (site only)

### Task 1: Name the codec (`lib/mp4Codec.ts`, new)

Read the first video track's sample-entry fourcc from an MP4 `File`. Walk the top-level boxes to find `moov`; it may sit at the end, since Resolve and Premiere do not always write it at the front. Each step reads only that box's 8–16-byte header plus the `moov` box itself via `file.slice`. Then walk `moov > trak > mdia > hdlr(vide) > minf > stbl > stsd` and return the entry type (`hvc1`, `hev1`, `avc1`, `av01`, `vp09`) or `null`.

- **Limit:** `moov` over 64 MiB returns `null`. It is a few MB on a 20-minute file.
- **Test (`mp4Codec.test.ts`):** a hand-built minimal box tree with `moov` after `mdat`, using a fake large `mdat` size so no real bytes are needed. One case each for `hvc1` and `avc1`; garbage input returns `null`; a 64-bit `largesize` box is handled.
- **Human label:** `codecLabel("hvc1"|"hev1") = "H.265"`, `avc1 = "H.264"`, `av01 = "AV1"`, `vp09 = "VP9"`, anything else `null`.

### Task 2: Record playability at upload (`lib/mediaChecks.ts`)

- `measureMedia` already loads the file into a detached `<video>`. Extend `Measure` with:
  - `codec: string | null` (from Task 1);
  - `playable: boolean | null`: `false` on the video's `error` event with `MEDIA_ERR_SRC_NOT_SUPPORTED`, or on `loadedmetadata` with `videoWidth === 0` (audio decoded but not video); `true` once a frame is decoded; `null` when unknown.
- `GradedChecks` gains `codec` and `playable`. The result is stored as part of `checks_json`. That is a free-form JSON string ≤ 16384 bytes, so the backend needs no change.
- Grading: no pass/fail for codec, since the streamer chose a bar, not a refusal.
- **Test:** the error event with code 4 gives `playable: false`; a decoded frame gives `true`; `codec` passes through.

### Task 3: The bar on top of the video (`components/editor/PlaybackBar.tsx`, new; `ReviewPlayer.tsx`)

- **Shows when either holds:**
  - (a) the viewer's own `<video>` fires `error` with code 4, or ends `loadedmetadata` with `videoWidth === 0`;
  - (b) the version's stored check says `playable: false` and this browser's `canPlayType` for that codec's MIME is `""`.
- **Text:**
  - `codec` is H.265: "This browser can't play H.265, open it in Chrome".
  - Otherwise: "This browser can't play this video's format, open it in Chrome".
  - When (b) holds but this browser *can* play it, show nothing.
- **Placement:** an overlay pinned to the top edge of the video area, `role="alert"`, not dismissable. It sits *on* the video, as the streamer asked.
- `IterationCard`: a small `rec` "Won't play in some browsers" chip when `checks.playable === false`.
- **Tests:** the bar text for each case; no bar when the stored flag is false but `canPlayType` says `"probably"`; the chip.

### Task 4: Export markers (`lib/markerExport.ts`, new; `components/editor/ExportMarkers.tsx`, new)

**Pure functions over `{comments, fps, startTc, title}`:**
- **Input:** only unresolved comments with `start_s != null` and not `carried` on this version. General and carried comments have no time on this version.
- **Name:** `"Fix · aryan"`. **Note:** body plus `" / "`-joined `author: reply` lines. Newlines become spaces, `|` becomes `/`, and a leading digit is prefixed with `_` (Resolve misreads it otherwise).
- **Frames:** `frames = Math.round(seconds * nominal)`, where `nominal = Math.round(fps)`, so 59.94 counts as 60. Duration is `max(1, end - start)` frames.
- **`toEdl`:** CMX3600. `TITLE:`, `FCM: NON-DROP FRAME`, then per marker:
  ```
  001  001      V     C        <tc> <tc+1> <tc> <tc+1>
   |C:ResolveColorRed |M:<name> - <note> |D:<frames>
  ```
  Colours: Fix `ResolveColorRed`, Question `ResolveColorYellow`, Keep `ResolveColorGreen`. The timecode is `startTc` plus the offset, at the nominal rate.
  - `shortcut: non-drop-frame only; a 59.94 DF timeline drifts ~0.06 s per minute (≈0.6 s at 10 min), add a DF switch if the editor reports it.`
- **`toXmeml`:** an FCP7 `xmeml version="5"` with one `<sequence>` named after the version: `<rate><timebase>nominal</timebase><ntsc>` (true for 23.976/29.97/59.94), a `<duration>`, and `<marker><name/><comment/><in/><out/></marker>` per marker. A range sets `out` to the end frame; a point gets `out = -1`. XML-escape every text value.
- **`toCsv`:** Markerbox's import format. **First step: read Markerbox's own docs for its CSV columns. Don't guess.** Write the format into a code comment with the doc URL. RFC 4180 quoting.

**Dialog** (button "Export markers" in the player's control row; both roles):
- Frame rate: prefilled from `checks.fps.value`, editable.
- Timeline start: default `01:00:00:00`.
- Both are remembered in `localStorage editor.markerExport` (a per-viewer convenience, wrapped in try/catch).
- Three buttons ("DaVinci Resolve (.edl)", "Premiere Pro (.xml)", "Markerbox CSV (.csv)") each download a Blob named `<tape> v<n> markers.<ext>`.
- One line of help under each: Resolve "Timelines › Import › Timeline Markers from EDL", Premiere "File › Import (makes a new sequence with the markers)", Markerbox "Markerbox panel › Import CSV".
- Disabled with "No open comments with a time" when the list is empty.

**Tests (`markerExport.test.ts`):**
- golden strings for one point and one range comment at 60 and 59.94 fps with start `01:00:00:00` (Fix at 3.2 s gives `01:00:03:12`);
- escaping (`|`, `<`, `"`, a leading digit, a newline);
- carried, resolved and general comments excluded;
- the dialog remembers its fields.

### Task 5: Iteration card gaps (from 6A)

- **Summary card:** under the thumbnail in `IterationCard`, with "N of M resolved" and open Fix/Question counts. The data is already in `useTape`; read it, don't re-fetch.
- **Menu item "Edit checklist":** opens `#/p/<p>/t/<t>/v/<i>` with the comment panel filtered to carried checkboxes. Add a `filter=carried` one-shot flag, the same way "Add comment" is passed today.
- **Tests:** the card text, and the menu item route.

**Deploy (6B):** on the Mac Mini, `cd ~/Documents/Site && git pull && npm run build`, then Ctrl+Shift+R on `/editor`.

---

## 6C: Side-by-side compare (site only)

### Task 1: `components/editor/CompareView.tsx` (new) and a route flag

- **Route:** `#/p/<p>/t/<t>/v/<i>?vs=<j>`, parsed in `useHashRoute`. Add it to the `Route.iteration` type as `vsIterationId?: number`.
- **Entry points:** a "Side by side" button next to the existing V hint, and the key `Shift+V`. Add it to `KeySheet`. The partner is the same one V uses.
- **Layout:** two `<video>` elements at 50/50 width, each served by the service worker at `/editor/media/<id>`. Both downloads stay in OPFS (`keepOnlyProject` keeps the whole project), so no cache change is needed. Each pane has a version label and its own `DownloadRing`.
- **One control bar:** play, pause, seek, speed and frame step apply to both. The **left** video is the clock; the right follows it.
  - On `seeked`, `ratechange`, `play` and `pause` of the left, mirror them to the right.
  - Every 500 ms while playing: if `|right - left| > 0.1 s`, set `right.currentTime = left.currentTime`.
- **Sound:** the left video is unmuted and the right muted. A "Sound: left/right" switch flips both.
- Comments show for the left (current) version only. Pressing C comments on the left.
- **Esc** leaves compare mode (back to `#/.../v/<i>`) before the shell zoom-out.
- The 6B bar applies per pane.
- **Tests (`CompareView.test.tsx`, with injected `setInterval`):**
  - mirror on play, pause, seek and rate;
  - drift correction above 0.1 s and none below it;
  - the sound switch;
  - Esc order;
  - one version shorter than the other: the right pauses at its end while the left plays on.
- **e2e:** one Playwright case in `e2e/` opens the compare route with the mock Drive and checks both videos advance together, in Chromium and Firefox.

**Deploy:** the site build, as in 6B.

---

## 6D: Comment attachments (backend + site)

### Task 1: Backend (`editor/editor_attachments.py`, new; routes in `editor_api.py`)

- **Table:**
  ```
  attachments(id, tape_id, comment_id NULL, reply_id NULL, author, name, mime, size,
              status CHECK IN ('uploading','ready','deleted'), drive_file_id, upload_url,
              created_at, updated_at)
  ```
  Created lazily (`_c()`), like the other modules.
- `POST /api/editor/attachments {iteration_id, name, size, mime}`, both roles:
  - Validate `mime ∈ {image/png, image/jpeg, image/webp, image/gif}`, `0 < size ≤ 20 MiB` and the name (≤ 200 chars, no path separators).
  - Refuse while the tape is deleting (`ensure_tape_not_deleting`).
  - Open a resumable session into the **tape's Drive folder** with `appProperties {"attachment_id": "<id>"}` and the site Origin; answer `{id, upload_url}`.
- `POST /api/editor/attachments/{id}/complete {file_id}`, author only:
  - Run `gdrive.get_file` to check the id, size and mime against the row.
  - On a mismatch, delete the file and answer 422.
  - Then set `ready`.
- `add_comment` / `add_reply` accept `attachment_ids: [int]` (≤ 4). Each must be `ready`, belong to the author and the tape, and be unattached. They are linked in the same transaction as the post. A retried post with the same `client_id` returns the original and links nothing again.
- `comments_for_iteration` and `GET /iterations/{id}` include `attachments: [{id, name, mime, size, drive_file_id}]` on each comment and reply.
- **Deletes:** the `tape_files` delete also deletes every attachment row's file for that tape: `DELETE /files/{id}`, 404 counts as success, marked `deleted`, the same per-file bracketed transactions as the iteration files. **Read `deletions.py` around `kind == "tape_files"` first and extend that loop; don't write a second one.**
- **Reconcile:** an attachment still `uploading` after 1 day is abandoned. Its row is marked `deleted`, and its Drive file, if any, is deleted. That is safe because it is unlinked and only its author ever saw it.
- **Tests:**
  - mime and size refusals;
  - the editor attaching another user's attachment is refused;
  - the idempotent post;
  - the tape_files delete removes the attachment files (mocked Drive), and a 404 is success;
  - complete with a mismatched size deletes the file and answers 422;
  - the 1-day reconcile.

### Task 2: Site

- `CommentComposer` and the reply box accept a paste, a drop or a 📎 button. Each image uploads at once through `lib/resumableUpload.ts` (reused) and shows a thumbnail with progress and a remove button. Send is disabled until every attachment is `ready`.
- The unsent queue stores attachment *ids*, never bytes. A comment queued while the Mac Mini is down sends later with the same ids.
- **Display:** a thumbnail row on the comment. Images load with `fetch(url, {headers: Authorization})` using the `media-link` token (the same token rule as 5B) into a `blob:` URL, revoked on unmount. Click opens a full-size view in the existing dialog style.
- No attachments on guest notes.
- **Tests:**
  - paste starts an upload;
  - Send waits for ready;
  - a rejected mime shows "Images only (PNG, JPEG, WebP, GIF), up to 20 MB";
  - the queued comment carries the ids;
  - the token never appears in an `img src`.

**Deploy (6D):**
1. Mac Mini: back up `config.json` and `app.db*`.
2. `cd ~/Documents/Stream && git pull`.
3. `launchctl kickstart -k gui/$(id -u)/com.dualbladex.backend`.
4. `curl localhost:8765/health`.
5. Then do the site build.

---

## 6E: Share links and guest notes (backend + site, plus one Google Cloud step)

**Google Cloud step for the streamer, done once (it is not done yet):** Google Cloud console, the same project as the Drive OAuth app.
1. Go to APIs & Services › Credentials › Create credentials › API key.
2. Under Application restrictions, choose Websites and add `https://hub.dualbladex.org/*`.
3. Under API restrictions, choose Google Drive API.
4. Paste the key into Settings › Editor app › "Drive public API key".

Steps go into `GDRIVE_SETUP.md`.

### Task 1: Config and Drive client

- `FIELDS`: `gdrive_public_api_key` in section Editor app, type `secret` (masked in Settings), help "Public, website-restricted API key for guest playback. Not your OAuth secret." Add it to `config.example.json` as `""`.
- `gdrive.py`:
  - `share_anyone(file_id) -> permission_id`: `POST files/{id}/permissions {type: "anyone", role: "reader"}` with `fields=id`.
  - `unshare(file_id, permission_id)`: `DELETE files/{id}/permissions/{pid}`. A 404 is success.
- **Tests:** aioresponses for both, including the 404.

### Task 2: Links (`editor/share_links.py`, new)

- **Table:**
  ```
  share_links(id, token_hash UNIQUE, iteration_id, created_by, created_at, expires_at,
              permission_id, state CHECK IN ('live','revoking','revoked'),
              revoke_reason, attempts, next_try)
  ```
- `create(iteration_id, hours, by, now)`, admin only:
  - `hours ∈ {1, 2, 10, 24, 168}`, default 2.
  - The iteration must be `ready`, and its tape not deleting.
  - Refuse when `gdrive_public_api_key` is empty ("Add the Drive public API key in Settings first").
  - `token = secrets.token_urlsafe(32)`; store only its sha256.
  - Call `share_anyone`, then insert, in two steps. If the insert fails after the share, `unshare` at once.
  - Answer `{url: f"{public_base_url}/s/#{token}", expires_at}`. **The token is returned once and never again.**
- `revoke(link_id, by, reason)` runs the same path as expiry.
- `run(now)` (scheduler job `share_links`): every `live` link with `expires_at <= now` goes to `revoking` (commit), then `unshare` (await), then `revoked`. On failure: back to `revoking` with `next_try = now + backoff_seconds(attempts)`. Never mark `revoked` without Drive answering 2xx or 404.
- **Untrusted clock:** add `scheduler.on_paused(callback)`. It runs once per pause, the first time `tick()` finds the clock behind (the same place `_paused_since` is set). Drive calls are allowed there because revoking early is safe. `main.py` registers `share_links.revoke_all_live("clock")`.
  - A link revoked while paused stays revoked. The streamer makes a new one.
  - **Tests:** `tick()` with the clock behind calls the callback once, even across several paused ticks; a later sane tick does not call it again; a new pause calls it again.
- **Iteration or tape files deleted:** `delete_done` for `tape_files` marks that tape's links `revoked` (the file is gone). No Drive call is needed.
- `list_live()` for the Live links list: `{id, tape, version, created_at, expires_at}`. Never the token.
- **Tests:**
  - a disallowed hours value is refused;
  - an editor is refused (403);
  - the token is not stored (only the hash is in the row);
  - expiry revokes, and a Drive 404 counts as revoked;
  - a Drive 500 retries with backoff and stays `revoking`;
  - a paused clock revokes all;
  - files delete marks revoked;
  - an insert failure after the share unshares.

### Task 3: Guest API (`editor/share_api.py`, new; `OPEN_PATHS += ["/s", "/api/share"]`)

All guest routes take the token in the **JSON body**, never the URL. A link counts only if `sha256` matches, `state == 'live'`, `expires_at > now` and the scheduler is sane.

- `POST /api/share/view {token}` returns `{title: "<tape> v<n>", file_id, api_key, duration_s, expires_at, codec, playable}`. It returns no project names beyond the tape title and no comments. A bad or expired token gets `404 {"error": "This link has expired or was revoked."}`, with one message for every failure so a guess learns nothing.
- `POST /api/share/notes {token, name, kind, body, start_s?, end_s?, client_id}`:
  - name 1–40 chars, trimmed; body 1–2000; kind in fix/keep/question; times inside `[0, duration]`.
  - Idempotent by `(link_id, client_id)`.
  - **Rate limit:** 30 notes per link per 10 minutes, and 200 per link in total. That is in-memory per link id plus a DB count for the total. Over the limit answers 429 "Slow down a little".
  - Stored in a new table, **not** in `comments`:
    ```
    guest_notes(id, link_id, iteration_id, name, kind, start_s, end_s, body, client_id,
                created_at, status CHECK IN ('pending','approved','rejected'), comment_id NULL)
    ```
  - Records the event `guest_notes_added` (actor `guest:<name>`, iteration id), which goes to the streamer only (Task 5).
- The guest sees only their own notes. The site keeps them in `localStorage` under the link (a per-viewer convenience); there is no server read of guest notes for guests.
- **Tests:**
  - an expired token, a revoked one, a paused clock and a wrong token all give the same 404;
  - the 30/10-min limit;
  - idempotent `client_id`;
  - the editor's session cookie cannot read `guest_notes` (Task 4's routes check admin);
  - `/s` and `/api/share` match `OPEN_PATHS` by whole segment (`/share-admin` does not).

### Task 4: Approval (admin routes in `editor_api.py`)

- `GET /api/editor/iterations/{id}/guest-notes` (admin): pending notes grouped as `[{name, notes: [...]}]`, oldest first.
- `POST /api/editor/guest-notes/approve {ids: [..]}` (admin): in one transaction, each still-`pending` id becomes a real comment through `add_comment(iteration_id, author="guest:<name>", ...)`, with `client_id = "guest-<note id>"` so a retry is idempotent. The note gets `status='approved', comment_id`.
  - The `comment_added` event is recorded with **actor = the streamer's username**, so the existing notifier sends the editor the usual digest, and the editor's bell shows it as the streamer's.
  - The comment's `author` stays `guest:<name>`, and the site renders it with a "guest" tag.
- `POST /api/editor/guest-notes/reject {ids}` (admin): `status='rejected'`.
- `comments_for_iteration` is unchanged, since unapproved notes are not in `comments`. The editor therefore never sees them. **Test this explicitly through the editor role.**
- `PATCH /comments/{id}` by author: a `guest:` author can never match a logged-in user, so approved guest notes are read-only. Replies and resolve work as on any comment.
- **Tests:**
  - approve two of three;
  - idempotent re-approve;
  - an editor gets 403 on all three routes;
  - the approved comment appears in the editor's `GET /iterations/{id}`;
  - an unapproved note does not;
  - the notifier routes the `comment_added` to the editor.

### Task 5: Bell and Discord for guest notes

- `editor_bell.feed`: `guest_notes_added` events show **only to admin usernames** (filter by `role_for_username(username) == "admin"`). Label: "Rahul left 3 guest notes on Jett dash v2".
- `notifier`: `guest_notes_added` goes to the streamer's digest (`digest_pending` for recipient `streamer`). It never goes to the editor, and it never includes note text, the same as comment digests.
- **Tests:**
  - the editor's feed never contains the kind;
  - the admin's does;
  - the digest goes to the streamer, and the editor gets nothing.

### Task 6: Site, streamer side

- A "Share" button on the version view (admin only) opens a dialog: expiry select (1 h, **2 h**, 10 h, 24 h, 7 days), Create, then the link shown once with Copy, and "Anyone with this link can watch v2 until 18:40."
- **Live links:** a section on the Storage page (admin) and in the Share dialog, with tape, version, "expires in 1 h 12 m" and **Revoke** (an `AskDialog` confirm).
- **Guest notes panel:** on the version view (admin only) when pending notes exist. A badge "Guest notes (5)" on the comment panel opens a list grouped under each name, with checkboxes, "Approve selected", "Reject selected" and "Select all from <name>". Clicking a note's time seeks.
- **Bell:** the new label. Clicking goes to the version with the guest-notes panel open.
- **Tests:**
  - the dialog default is 2 h;
  - the link is shown once (gone after closing);
  - Revoke calls the API;
  - grouped approval sends the chosen ids;
  - the editor role renders no Share button and no guest panel.

### Task 7: Site, guest page `/s`

- **New static route `app/s/page.tsx`**, with no login and no editor shell. It reads the token from `location.hash`, then immediately runs `history.replaceState(null, "", "/s")` so the token leaves the address bar and history. It keeps the token only in memory, then calls `/api/share/view`.
- **Playback:** `<video src="https://www.googleapis.com/drive/v3/files/<file_id>?alt=media&key=<api_key>">`. That needs no service worker and no OPFS, and the browser handles Range. The 6B bar logic applies, using `codec` and `playable`.
- **Notes:**
  - a name field (remembered in `localStorage`);
  - C to capture the time, a range, or general;
  - kind buttons;
  - "Your notes" lists what this browser sent.
  - The composer and player keys are reused from 5B where they take props; don't fork them. Use `isTyping` for keys.
- **Failure states:** an expired or revoked link shows "This link has expired. Ask DualBladeX for a new one." A 403 `downloadQuotaExceeded` from Google shows "Too many people watched this today; try again later."
- **Backend CSP for `/s`:** `media-src 'self' blob: https://www.googleapis.com https://*.googleusercontent.com`. Add those hosts to `PAGE_CSP`'s `media-src`; they are already in `connect-src`. Add `Referrer-Policy: strict-origin-when-cross-origin`, the default, stated explicitly so the key's referrer restriction keeps working.
- **Tests:**
  - the hash is cleared;
  - the token is sent only in a body;
  - the expired message;
  - notes post with `client_id` and survive a failed POST through the unsent queue (reuse `unsentQueue`, keyed per link).
- **e2e:** a mock `/api/share/view` plus mock Drive media; the guest page plays and posts a note (Chromium and Firefox).

**Deploy (6E):**
1. Do the Google Cloud API-key step above and paste the key in Settings.
2. Mac Mini: back up config and `app.db*`, `git pull`, restart the backend, `curl /health`.
3. Site: `git pull && npm run build`.
4. Live test: make a 1 h link, open it in a private window on a phone, leave a note, approve it, and check the editor sees it.
5. Revoke the link and check the phone can no longer start the video. Note that a video already playing may continue from its buffer.

---

## 6F: Hinglish auto transcript (backend + gaming PC + site), POSTPONED

Postponed 2026-10-31 (no access to the gaming PC). Kept here so it can be picked up as written.

### Task 0: Spike on the gaming PC (gates the rest)

Run Oriserve `Whisper-Hindi2Hinglish-Prime` (or `-Swift` if VRAM is short) on one real 10-minute review version:
- (a) convert it to CTranslate2 with `ct2-transformers-converter --quantization float16` and run it under `faster-whisper`;
- (b) if the conversion fails, run it under `transformers`' pipeline on CUDA.

Record wall time, VRAM, and a quality sample for the streamer. **The streamer approves the quality before Task 1.** If neither path works, stop and report; the fallback (stock large-v3, Devanagari output) needs the streamer's decision. That needs a CUDA GPU on the PC, and its model is checked in this step.

### Task 1: Backend (`editor/transcripts.py`, new; routes)

- **Tables:**
  ```
  transcripts(iteration_id PRIMARY KEY, status CHECK IN ('queued','running','done','failed'),
              segments_json, model, error, attempts, claimed_at, created_at, updated_at)
  ```
  plus `meta auto_transcript` (`"1"`/`"0"`, default off).
- **Queue:** after `complete_iteration` (in `editor_service.complete_iteration_upload`, so reconcile is covered too), if auto is on, insert `queued` in the same transaction. VODs never queue, because only iterations come through this path.
- **Editor routes (both roles):**
  - `GET/PUT /api/editor/transcripts/auto {on}`;
  - `POST /api/editor/iterations/{id}/transcript`: "Make transcript", which queues or re-queues a `failed` one;
  - `GET /api/editor/iterations/{id}/transcript` returns `{status, segments?}`.
- **PC routes** (`/api/uploader/transcripts/...`, already under `OPEN_PATHS` `/api/uploader`, `X-Agent-Secret`):
  - `POST next`: claims the oldest `queued` (or a `running` one claimed more than 2 h ago) as `running`, and answers `{iteration_id, file_id, size, access_token}`. The token comes from `gdrive.access_token()`, the same ≥10-minute token rule as `media-link`. The PC is inside the agent-secret trust boundary.
  - `POST {id}/token`: a fresh token for a long download.
  - `POST {id}/done {segments: [{start, end, text}], model}`: validate it is ≤ 1 MB and sorted, then store. Records the event `transcript_ready`, with actor `agent` and no DM.
  - `POST {id}/failed {error}`: `attempts += 1`; back to `queued` while `attempts < 3`, else `failed`.
- `editor_comments.search` also searches `transcripts.segments_json` (LIKE over text, per segment), giving hits `{kind: "transcript", iteration_id, start_s, snippet}`. Keep the 50-hit limit.
- **Deletes:** the `tape_files` delete removes that tape's transcripts rows. They are small, but history beyond comments was not promised.
- **Tests:**
  - auto off queues nothing;
  - auto on queues on complete and on reconcile's complete;
  - a VOD never queues;
  - claim is exclusive (two `next` calls give different jobs or none);
  - a stale `running` gets reclaimed;
  - three failures end in `failed`;
  - an oversized or unsorted `done` is refused;
  - search returns a transcript hit;
  - a wrong secret gets 403.

### Task 2: Gaming PC (`gaming pc/transcriber.py`, new; `requirements-transcribe.txt`, new)

- **Loop**, started by the existing logon task. Extend `install_uploader.py` to add a second `pythonw transcriber.py` task, and add `--uninstall` support.
  1. If `live_gate.should_pause()` says live, sleep 60 s.
  2. Otherwise call `next`. Nothing to do: sleep 120 s.
  3. On a job, download `alt=media` to `%TEMP%\dbx-transcribe\<id>.mp4` with the token in an `Authorization` header, in chunks, re-asking `token` on a 401.
  4. Run `ffmpeg -vn -ac 1 -ar 16000` to make a `.wav`.
  5. Transcribe, mapping `language="hi"` per the spike.
  6. Call `done`, then delete both temp files. Delete them on any failure too (`finally`).
- **Mid-job checks:** pause between steps while live. If the stream goes live mid-transcribe, finish the current file. It is GPU work and only a few minutes, and the streamer's upload bandwidth is untouched once downloaded. Document this.
- **Never logs** the token, the file id or transcript text. The token never goes on the ffmpeg command line, because the download is done in Python.
- `requirements-transcribe.txt` is separate from `requirements.txt`, so the OCR agent stays light. `UPLOADER_SETUP.md` gets a "Transcripts" section covering ffmpeg on PATH, the CUDA install and the first-run model download size.
- **Tests (`test_transcriber.py`, with injected http, ffmpeg and model):**
  - the job flow calls `done` with segments;
  - temp files are deleted on success and on an exception;
  - a 401 refreshes the token once;
  - live pauses before claiming;
  - `failed` is posted with the error class name only.

### Task 3: Site

- **Top-bar switch:** an "Auto transcript" toggle in `EditorShell`'s top bar next to the bell, both roles, with tooltip "Transcribe new versions in Hinglish on the gaming PC". It reads and writes `/transcripts/auto`.
- **Version view:** a "Transcript" tab beside Comments.
  - States: none ("Make transcript" button), queued ("Waiting for the gaming PC"), running, done, failed ("Failed: try again").
  - Done: lines `m:ss  text`. Click seeks; the current line is highlighted while playing (no auto-scroll under reduced motion); "Comment here" on a line opens the composer at that time.
  - Poll every 15 s while queued or running.
- **Search:** transcript hits show with a "transcript" label and jump to the time.
- **Tests:**
  - the toggle round-trips;
  - each tab state;
  - click seeks;
  - the polling stops at done;
  - a search hit route.

**Deploy (6F):**
1. Mac Mini: back up, `git pull`, restart, `/health`.
2. Site: build.
3. Gaming PC:
   1. `git pull`.
   2. `pip install -r requirements-transcribe.txt`.
   3. Install ffmpeg and check the CUDA drivers.
   4. Run `python install_uploader.py` again to add the transcriber logon task.
   5. Sign out and in, or start `pythonw transcriber.py` once.
4. Turn the switch on, upload a short version, and check that a transcript appears.

---

## As built (2026-11-01), where it differs from the tasks above

- 6B: the Markerbox CSV is **not built**: Markerbox documents no CSV format. Waiting on one file exported from
  Premiere (File > Export > Markers) by the editor; the .edl and .xml downloads ship.
- 6C: the side-by-side view has no comment panel (comment from the single-version view); route
  `#/p/<p>/t/<t>/v/<i>/vs/<j>`, Esc goes to `v/<i>`; one Playwright case.
- 6D: attachments live in one `Attachments` Drive folder per tape (meta `attachments_folder_<tape>`); the tape's
  file delete removes that folder as a `folder` row (no change to `delete_files`' CHECK). Drive flows are in
  `editor_service` (start/complete/attachment_media_link), rows in `editor_attachments`. Stale uploads are dropped
  lazily on the next start, not by a scheduler job.
- 6E: the share URL is `/s#<token>`. The fragment is **kept** in the address bar (not cleared), so a reload still
  works; it never reaches a server or Referer. Guest notes waiting to be sent are stored by file id, never with the
  token. No Playwright case for `/s` (it plays from googleapis.com, which the mock Drive cannot stand in for).
  `find()` checks `scheduler.clock_is_sane(now)` directly, so a link is refused even before the first tick.

## Review focus (whole plan)

1. **A share link outliving its expiry:** the Mac Mini off at expiry, the clock booting in 2001, Drive 500s on revoke. Pinned in 6E task 2.
2. **The editor seeing an unapproved guest note,** through the API, the bell, a DM, search or the fix list. Pinned in 6E tasks 4 and 5, plus a test that `search` does not reach `guest_notes`.
3. **The share token or OAuth token leaking:** into a URL the server sees, a log, `Referer`, `localStorage`, or an error. Pinned in 6E tasks 3 and 7 and 6F task 2.
4. **Attachment files left on Drive after a tape's files are deleted,** or an attachment from one user attached by another. Pinned in 6D task 1.
5. **Compare drifting apart,** or the right pane's sound leaking. Pinned in 6C.
6. **The GPU job touching the stream:** the transcriber must not claim while live. Pinned in 6F task 2.

## Not in this plan (declined or deferred)

- H.264 conversion of H.265 uploads (the streamer chose a bar).
- Guests replying, resolving or attaching, or seeing other guests' notes.
- Drop-frame EDL.
- A Premiere UXP panel (Markerbox covers it).
- Transcribing VODs.
- Devanagari output.
