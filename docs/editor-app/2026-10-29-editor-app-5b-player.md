# Editor App Plan 5B: Review Player and Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the version-details stub at `#/p/<p>/t/<t>/v/<i>` with the review player. By default it downloads the whole file into the browser's private disk storage (OPFS) before playback, and **Start watching now** plays at once from what has arrived plus Drive. It hosts comments: C to capture the time, ranges, general comments, scrub-bar markers, a list that follows playback, threads, resolve, carried checkboxes and New badges. It also covers player keys with a `?` sheet, resume position, compare versions, an unsent-comment queue that survives the Mac Mini being down, and search.

**Architecture:**
- **The player** is one view component (`ReviewPlayer`) over three hooks: `useMediaFile` (chunked OPFS download, resume, cleanup), `useComments` (load and mutate comments through the unsent queue) and `usePlayerKeys` (keyboard).
- **Download:** bytes come straight from Google (`GET https://www.googleapis.com/drive/v3/files/<id>?alt=media` with the `media-link` token in an `Authorization` header). They arrive in 32 MiB ranged chunks, each saved as its own OPFS file. Separate files, because a growing single OPFS file is rewritten on every `createWritable` commit, and a reader cannot see a half-written one.
- **Playback** always goes through a service worker (`/editor/media-sw.js`). The `<video>` points at the same-origin URL `/editor/media/<iterationId>`; the worker answers each range request from the chunk files when they are present, and otherwise fetches that range from Drive using the token the page hands it in memory. Download-first and watch-now are one code path: the only difference is whether Play is offered before 100%. Nothing large is held in RAM.
- **Backend:** four small things. Idempotent comment and reply posts (`client_id`), a per-iteration "seen" marker for New badges, a CSP widening for Google's download redirect host, and a `Service-Worker-Allowed: /editor` header on the worker script.

**Tech Stack:** Next.js 16 static export, React, Tailwind tokens from `app/globals.css` (`ink`, `bezel`, `seam`, `phosphor`, `field`, `rec`, `amber`, `signal`, `dim`; fonts `stencil`/`osd`/`mono`), vitest + Testing Library, a plain-JS service worker in `public/editor/`. Backend: Python aiohttp in `mac mini/Stream` (`core/`, `editor/`).

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md`, sections "Flow 4. Review", "Flow 5. Threads and notifications", the "Decisions made after the builder check" bullets on carried comments and review devices, and "Left to the builder". Roadmap rows: "Editor frontend" items marked `[ ] 5` (player, comments in the player, keys/resume/compare, unsent queue, thread UI/New badges/search). 5A plan: `docs/superpowers/plans/2026-10-28-editor-app-5a-site.md`; its shell, router (`Route.iteration.seekS`), `editorApi`, `IterationCard`, `FixListView` and `useTape` are reused, not rebuilt.

## Global Constraints

- Static export only. The player lives inside the existing `/editor` page and hash router; `#/p/1/t/2/v/3@222.5` seeks to 222.5 s.
- **Default: the whole file is in OPFS before Play is offered** (spec: full quality, no stutter). Progress is shown as "Loading v3 · 1.2 of 3.4 GB · 41%".
- **Start watching now** (streamer, 2026-10-29, for slow connections) offers Play at any point. Downloaded parts play from disk, the rest streams from Drive at full quality, and the full download carries on in the background. It may pause to buffer when the connection is slower than the video's bitrate. Drive's own player is never used (it re-encodes, and its time cannot be read for comments).
- **Download indicator** (streamer, 2026-10-29): a `DownloadRing` icon in the control row fills with the downloaded share, with the tooltip and `aria-label` "Downloaded 1.2 of 3.4 GB (35%)". At 100% it becomes a check, "Downloaded · plays from this computer". The scrub bar shades the downloaded parts.
- The access token from `GET /api/editor/iterations/{id}/media-link` (`{url, access_token, expires_in, drive_url}`) lives only in memory, in the page and in the service worker. It travels only in an `Authorization: Bearer` header or a `postMessage` to the worker, never in a URL, `localStorage`, an OPFS name, a log or error text. It is refreshed by calling `media-link` again when fewer than 60 s remain or a request answers 401.
- **OPFS cache policy** (streamer, 2026-10-29, final: "delete the videos as soon as I exit the project, i.e. VOD or clip"; a browser extension and close-time deletion were considered and declined).
  - **Layout:** one directory per version, `it-<projectId>-<iterationId>-<driveFileId>/`, holding chunk files `c00000`, `c00001`, … of `CHUNK_BYTES` each (the last one shorter). A chunk counts as present only when its size is exactly the expected size: `createWritable` commits on `close()`, so a cut-off write leaves the old (empty) content, never a half chunk. The download is complete when every chunk is present.
  - **Directories are kept while the route is inside their project** (`project`, `tape` or `iteration` views with that `projectId`), so reopening a version or comparing is instant.
  - **Any other route deletes them, unfinished downloads included:** another project, or Home, Fix list, To-do or Storage.
  - **The same check runs when the page loads,** which covers a closed tab: what was left behind is removed on the next visit unless it opens in the same project. A reload or deep link into the same project keeps them, so a cut-off download resumes.
  - **Running out of space inside a project:** when a new download does not fit, the least recently opened versions of that project are deleted first. The current version and its compare partner are never deleted. The download is refused with `NotEnoughStorage` only if it still does not fit.
  - **A directory whose `driveFileId` differs from the iteration's current one** is deleted.
- Comments API (read `editor/editor_api.py` and `editor/editor_comments.py`; never guess fields):
  - `GET /iterations/{id}` returns `{iteration, comments, position_s}`. Each comment carries `{id, iteration_id, author, kind, start_s, end_s, body, created_at, updated_at, resolved, resolved_by, resolved_at, origin_iteration_id, origin_version, carried, replies: [{id, comment_id, author, body, created_at}]}`.
  - `POST /iterations/{id}/comments {kind, body, start_s?, end_s?, client_id}`.
  - `PATCH /comments/{id} {body?, kind?}` is author only; anyone else gets a 403.
  - `POST /comments/{id}/replies {body, client_id}`.
  - `PUT /iterations/{iid}/comments/{cid}/resolved {resolved}` is **admin only**.
  - `PUT /iterations/{id}/position {position_s}`.
  - `GET /search?q=` returns `{hits: [{kind, comment_id, iteration_id, tape_id, project_id, version, snippet, start_s}]}`.
- Kinds are `fix`, `keep`, `question`, labelled "Fix", "Keep" and "Question".
- **A carried comment has no timestamp on this version.** It gets no scrub marker and is never seeked to on this version. It shows "from v<origin_version> at mm:ss" (or "from v1, general"), and clicking that goes to `#/p/<p>/t/<t>/v/<origin_iteration_id>@<start_s>`.
- Time format: `m:ss` under an hour, `h:mm:ss` otherwise, plus `.f` tenths only in the composer's time fields.
- Editor role: no Resolve control (read-only state shown), no Start over. The server refuses both anyway, and a 403 that comes back is shown.
- Keyboard handling sits on the player container. A key typed in an input/textarea/select/contentEditable is ignored, using the same `isTyping` rule as `EditorShell`. `Esc` closes the `?` sheet or the composer first and stops propagation; only then does it reach the shell's zoom-out.
- Devices: Chrome and Firefox (current) on desktop or laptop. No Safari or mobile.
- New `lib` modules each get a test; components that hold logic get a test. The site suite, `node_modules/.bin/tsc --noEmit`, `npm run lint` (0 errors) and `npx next build` pass after every site task.
- Look: the site's existing tokens and the dashboard panel style (`border border-seam bg-bezel`). Kind colours: Fix `rec`, Keep `signal`, Question `amber`. `prefers-reduced-motion` turns off the list's smooth auto-scroll and any transitions.
- Commits use `-c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com`, stage files by name (there is `*:Zone.Identifier` junk), and carry the session trailers. Branch `editor-app-plan-5b` in both `mac mini/Stream` and `mac mini/Site`. Push only with the user's go-ahead.
- Backend tests: `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings` (12 known `test_credit_ocr.py` failures). Site: `npx vitest run`.

## Review Focus

1. **The download is cut mid-way:** the laptop sleeps, Wi-Fi drops, the token expires on a 4 GB file, or the tab reloads. Reopening the same version continues from the chunks already in OPFS. It never restarts from zero and never offers download-first Play on a half file. Pinned in Task 2 (resume from present chunks, 401 refresh, exact-size rule).
2. **The disk is too small for the file:** `navigator.storage.estimate()` shows less free space than the file needs. The player says "Not enough browser storage: needs 3.4 GB, 1.1 GB free" before downloading, instead of failing at 90%; **Start watching now** still works (it streams what is missing). A `QuotaExceededError` mid-write gives the same message and keeps the chunks so far. Pinned in Tasks 2 and 3.
3. **A comment is typed while the Mac Mini is down, or the tunnel answers 502/530:** the comment is kept, shown as "Not sent yet", and sent once the backend answers, exactly once. Pinned in Task 5: the queue survives reload, and `client_id` dedupes on the server (Task 1).
4. **The editor presses keys in the reply box,** e.g. space, `c` or `?`. The text is typed and the player does nothing. Pinned in Task 4.
5. **Seeking ahead while watching now:** jumping to a part not yet downloaded plays from Drive. A range request that straddles a downloaded chunk and a missing one is answered correctly (bytes from disk, then from Drive). The service worker being restarted by the browser (it drops in-memory state when idle) re-asks the page for a token instead of failing. Pinned in Task 3. (The carried-comment rule, "from v1 at 3:42" with no marker on v3, is pinned in Task 5.)

---

### Task 1: Backend: idempotent comment posts, iteration seen marker, CSP, service-worker scope

**Files:**
- Modify: `editor/editor_comments.py`, `editor/editor_personal.py`, `editor/editor_api.py`, `core/server.py`
- Test: `tests/test_editor_comments.py`, `tests/test_editor_api.py`, `tests/test_security_headers.py` (whichever file already pins `PAGE_CSP`; `grep -rn PAGE_CSP tests`)

**Interfaces (produces):**
- `comments.client_id TEXT` and `replies.client_id TEXT`, each with `CREATE UNIQUE INDEX IF NOT EXISTS ... WHERE client_id IS NOT NULL`. The columns are added to an existing `app.db` with `ALTER TABLE ... ADD COLUMN` when `PRAGMA table_info` lacks them, run in `_cc()` before any transaction.
- `ec.add_comment(..., client_id: str | None = None)` and `ec.add_reply(..., client_id: str | None = None)`: a repeat with the same `client_id` returns the existing row and records **no** second event. The route returns `201` on the first post and `200` on a repeat.
- The route validates `client_id`: optional, `^[A-Za-z0-9_-]{8,64}$`, else 400 "client_id is not valid".
- New table `iteration_seen(username TEXT, iteration_id INTEGER, seen_at REAL, PRIMARY KEY(username, iteration_id))`. `ep.mark_iteration_seen(username, iteration_id, now=None) -> float | None` returns the previous `seen_at` (None on first visit) and stores `now`.
- `POST /api/editor/iterations/{id}/seen` (both roles) returns `{"previous_seen_at": float | null}`. It is a separate call because `GET /iterations/{id}` is also fetched by every `IterationCard`, and a card mount must not count as a visit.
- `PAGE_CSP` `connect-src` becomes `'self' https://www.googleapis.com https://*.googleusercontent.com`, in case Drive's `alt=media` redirects to a content host.
- `core/server.py`'s `on_response_prepare` adds `Service-Worker-Allowed: /editor` to the response for exactly `/editor/media-sw.js`. The script sits under `/editor/` (inside `EDITOR_PATHS`, so the editor can fetch it), and its default scope `/editor/` would not cover the page `/editor` itself.

- [ ] **Step 1: Write the failing tests.**
  - `test_add_comment_with_same_client_id_returns_existing_and_one_event`: two calls with `client_id="abcdefgh1"` return the same `id`, and `events_since(0)` holds one `comment_added`.
  - `test_add_reply_same_client_id_dedupes`, the same check for replies.
  - `test_client_id_columns_added_to_old_db`: create `comments`/`replies` with the old DDL, call `_cc()`, and `PRAGMA table_info` now lists `client_id`.
  - `test_comment_route_201_then_200_on_repeat` and `test_comment_route_rejects_bad_client_id` (`"x"` gives 400).
  - `test_mark_iteration_seen_returns_previous`: `None` first, then the earlier `now`, and per-user.
  - `test_seen_route_both_roles`.
  - `test_page_csp_allows_googleusercontent`.
  - `test_media_sw_gets_service_worker_allowed` and `test_other_editor_files_do_not` (a fake `out/editor/media-sw.js` served through `site_server`).
- [ ] **Step 2: Run them.** Expected: FAIL (unknown kwarg `client_id`, 404 route, CSP assertion).
- [ ] **Step 3: Implement.** Inside `db.transaction()`, look the `client_id` up first and return early if found, so the existing row needs no exception handling.
- [ ] **Step 4: Run the backend suite.** Expected: everything passes except the 12 known failures.
- [ ] **Step 5: Commit.** `git add` the files above by name, then commit with the message "Editor: idempotent comment posts, iteration seen marker, CSP for Drive media, service-worker scope".

### Task 2: Site: chunked OPFS download and `useMediaFile`

**Files:**
- Create: `lib/opfsMedia.ts`, `lib/useMediaFile.ts`
- Modify: `lib/testUtils.ts` (fake OPFS directory, fake `estimate`)
- Test: `lib/opfsMedia.test.ts`, `lib/useMediaFile.test.ts`

**Interfaces (produces):**
- `CHUNK_BYTES = 32 * 1024 * 1024`, `chunkCount(size)`, `chunkName(i)` (`c` plus five digits) and `dirName(projectId, iterationId, fileId)`.
- `presentChunks(dir, size): Promise<boolean[]>`: a chunk is present when its file's size is exactly the expected size.
- `downloadToOpfs(opts: {projectId: number; iterationId: number; fileId: string; size: number; getLink: () => Promise<{url: string; access_token: string; expires_in: number}>; onProgress(present: boolean[]): void; signal: AbortSignal; root?: FileSystemDirectoryHandle; now?: () => number}): Promise<void>`. The flow:
  1. Open or create the version directory under `root ?? await navigator.storage.getDirectory()`.
  2. For each missing chunk in order, fetch `Range: bytes=<i*CHUNK>-<min(size, (i+1)*CHUNK)-1>` with the bearer header.
  3. Write the chunk with `createWritable()`, `write(blob)`, `close()`.
  4. Report progress after each chunk.
- Error handling:
  - A 401 refreshes the link once per chunk, and the link is refreshed ahead of time when `expires_in` minus elapsed is under 60 s.
  - A network error or 5xx retries after `min(2 * 2 ** n, 60)` s, waiting for `online` while `navigator.onLine` is false.
  - A 403 or 404 throws `MediaGone("This file is no longer on Drive")`.
  - A 200 instead of 206 throws an error (no range support), never saving a whole file as one chunk.
- Space check: before the first missing chunk, `navigator.storage.estimate()` must show `quota - usage >= missing bytes`. Otherwise call `freeSpaceFor`, then throw `NotEnoughStorage(needBytes, freeBytes)` ("Not enough browser storage: needs X GB, Y GB free") if it is still short. A `QuotaExceededError` from a write throws the same and keeps the chunks so far.
- `keepOnlyProject(projectId: number | null, root?)` deletes every `it-*` directory whose project is not `projectId`, and everything when it is `null`. `EditorShell` calls it on mount and on every route change, with the route's `projectId`, or `null` for views without one. Calls are serialised: a new call waits for the previous one.
- `freeSpaceFor(needBytes, projectId, protect: string[], root?)` deletes that project's version directories, least recently opened first, skipping the `protect` prefixes, until `estimate()` shows `needBytes` free. "Opened" is an in-memory map (prefix → ms), written by `useMediaFile`.
- `useMediaFile(iteration: {id, drive_file_id, size} | null, projectId: number, comparePartnerId: number | null): {state: "checking" | "downloading" | "complete" | "error", present: boolean[], doneBytes: number, total: number, error: string | null, getLink(): Promise<Link>}`.
  - On open, it records the opened time, deletes any directory for the same iteration with another `driveFileId`, then downloads.
  - Unmount aborts the download. It deletes nothing itself; `keepOnlyProject` and `freeSpaceFor` own deletion.
  - `getLink` calls `editorApi("/iterations/<id>/media-link")` and caches the result in memory until 60 s before expiry. Task 3 hands it to the service worker.
- [ ] **Step 1: Write the failing tests** against the fake OPFS directory, a fake `fetch` and a fake `estimate`.
  - `downloads in 32 MiB ranges into chunk files`: 70 MiB gives 3 requests with ranges `0-33554431`, `33554432-67108863` and `67108864-73400319`, and 3 files `c00000`–`c00002` of the right sizes.
  - `resumes after the present chunks`: with `c00000` full and `c00001` empty, the first request is `bytes=33554432-67108863`.
  - `a short chunk file counts as missing and is fetched again`.
  - `refreshes the link on 401 and never puts the token in the URL`: every requested URL lacks `access_token`, and the header has `Bearer t2` after the refresh.
  - `throws NotEnoughStorage before fetching when freeSpaceFor cannot make room`.
  - `keeps written chunks on QuotaExceededError`.
  - `MediaGone on 404`.
  - `refuses a 200`.
  - `complete version fetches nothing`.
  - `keepOnlyProject(7) deletes project 3's directories and keeps project 7's`; `keepOnlyProject(null) deletes all`; two quick calls run in order.
  - `freeSpaceFor deletes that project's least recently opened first, never the protected prefixes, and never another project's files`.
  - `useMediaFile`: `reports complete when every chunk is present`; `unmount deletes nothing`; `a new drive file id for the same iteration replaces the old directory`.
- [ ] **Step 2: Run them.** `npx vitest run lib/opfsMedia.test.ts lib/useMediaFile.test.ts`. Expected: FAIL (module not found).
- [ ] **Step 3: Implement** both modules.
- [ ] **Step 4: Run the site suite, tsc and lint.** Expected: all pass.
- [ ] **Step 5: Commit** "Editor: download versions into OPFS in chunks with resume".

### Task 3: Site: playback service worker, Start watching now, download ring

**Files:**
- Create: `public/editor/media-sw.js`, `public/editor/media-range.js` (pure range logic: the worker loads it with `importScripts("/editor/media-range.js")`, and the test imports the same file, which ends with `if (typeof module !== "undefined") module.exports = {...}`), `lib/useMediaWorker.ts`, `components/editor/DownloadRing.tsx`
- Test: `lib/mediaRange.test.ts` (imports `../public/editor/media-range.js`), `lib/useMediaWorker.test.ts`, `components/editor/DownloadRing.test.tsx`

**Interfaces (produces):**
- `mediaUrl(iterationId) = "/editor/media/" + iterationId`.
- `useMediaWorker(): {ready: boolean, error: string | null, provide(iterationId, info: {projectId, fileId, size, mime, getLink})}`:
  - registers `/editor/media-sw.js` with `{scope: "/editor"}` and waits for it to control the page; the first visit may need `clients.claim()` in the worker's `activate`.
  - `provide` posts `{type: "media", iterationId, projectId, fileId, size, mime, link}` to the controller.
  - It answers the worker's `{type: "need-link", iterationId}` messages by calling that version's `getLink()` and posting a fresh link.
  - Without service worker support it sets `error` to "This browser can't play from the download; use Chrome or Firefox". The download still runs, and Download / Open in Drive remain.
- `mediaRange` (pure, no DOM), used by the worker:
  - `parseRange(header, size) → {start, end}`. A missing header means the whole file; `bytes=a-` and `bytes=a-b` are clamped to the size; the cap per response is `CHUNK_BYTES` from `start`.
  - `planRange(start, end, present: boolean[]) → [{source: "disk", chunk, from, to} | {source: "drive", from, to}]`: consecutive pieces in order, with neighbouring Drive pieces merged into one request.
- Worker behaviour: `fetch` events for `/editor/media/<id>` only; everything else falls through to the network untouched.
  - It answers `206` with `Content-Range: bytes start-end/size`, `Content-Length`, `Accept-Ranges: bytes` and `Content-Type: <mime>`.
  - The body is a `ReadableStream` that pipes each planned piece in turn: disk pieces from `(await chunkHandle.getFile()).slice(...)`, Drive pieces from `fetch(url, {headers: {Authorization, Range}})`.
  - It never writes to OPFS; only the page's download does.
  - Its state is a `Map` of iterationId to info, refreshed by `media` messages. When the info or a valid token is missing (the worker was restarted, or the token is about to expire), it posts `need-link` to the requesting client (`event.clientId`, or every window client from `clients.matchAll()` when that is empty) and waits up to 15 s, else answers 503.
  - A Drive 401 triggers one `need-link` and a retry.
  - It logs nothing that contains the token.
- `ReviewPlayer` integration (wired in Task 4):
  - Play is offered when `useMediaFile.state === "complete"`, or after the person presses **Start watching now**, which sits next to the progress and is disabled until the worker is ready.
  - Once watching, the download keeps running. A `NotEnoughStorage` error still lets you watch now, with the note "Streaming only: not enough browser storage to save it".
- `DownloadRing({doneBytes, total})`: an SVG ring of 18 px whose stroke shows `doneBytes / total`, with `role="img"`, the `aria-label` and `title` "Downloaded 1.2 of 3.4 GB (35%)", and at 100% a check with "Downloaded · plays from this computer". GB values use one decimal.

- [ ] **Step 1: Write the failing tests.**
  - `parseRange`: `bytes=0-` gives `0..CHUNK-1` on a big file; `bytes=100-199` gives 100..199; a missing header gives `0..min(size, CHUNK)-1`; a start past the size is invalid (the worker answers 416).
  - `planRange`: present `[true, false, true]` with a range across all three gives disk, drive, disk; two neighbouring missing chunks give one Drive piece.
  - `useMediaWorker`: registers with scope `/editor`; a `need-link` message is answered with a fresh link; `provide` posts the info (fake `navigator.serviceWorker`); unsupported browsers set the error.
  - `DownloadRing`: label text at 35%, and the check plus done label at 100%.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.** The worker stays plain JS with no build step, kept small, with the range logic in `media-range.js` so it is unit-tested.
- [ ] **Step 4: Run the site suite, tsc, lint and build.** Confirm `out/editor/media-sw.js` and `out/editor/media-range.js` exist.
- [ ] **Step 5: Commit** "Editor: play through a service worker, Start watching now, download ring".

### Task 4: Site: review player, controls, keys, resume position

**Files:**
- Create: `components/editor/ReviewPlayer.tsx`, `components/editor/ScrubBar.tsx`, `components/editor/KeySheet.tsx`, `lib/usePlayerKeys.ts`, `lib/timecode.ts`
- Modify: `components/editor/EditorShell.tsx` (the `iteration` route renders `ReviewPlayer` instead of `IterationDetails`; `IterationDetails` stays exported for the card's details; calls `keepOnlyProject(route projectId ?? null)` on mount and on every route change)
- Test: `lib/usePlayerKeys.test.ts`, `lib/timecode.test.ts`, `components/editor/ScrubBar.test.tsx`, `components/editor/ReviewPlayer.test.tsx`

**Interfaces:**
- `formatTime(s: number, tenths = false): string` and `parseTime(text: string): number | null`. Examples: `formatTime(222.5)` is `"3:42"`, `formatTime(3725)` is `"1:02:05"`, `formatTime(222.54, true)` is `"3:42.5"`; `parseTime("3:42.5")` is `222.5`, `parseTime("bad")` is `null`.
- Layout of `ReviewPlayer({projectId, tapeId, iterationId, seekS, role, username})`:
  - **Header:** the version title, `CheckBadges` from 5A, and Download / Open in Drive links.
  - **Before playing:** the loading panel with progress, **Start watching now** (Task 3) and a Cancel button that goes back to the tape. On error, the message plus **Try again**.
  - **Playing:** `<video src={mediaUrl(iterationId)}>` (no native `controls`; the URL from Task 3) with `ScrubBar` and a control row: play/pause, time `3:42 / 10:05`, speed, the `DownloadRing` (Task 3), `?` and fullscreen. Fullscreen targets the player container, so comments stay reachable via exit.
  - **Beside the video:** the comment side panel slot (Task 5).
- Speeds are `[0.25, 0.5, 1, 1.5, 2]`. Frame step is `1 / fps`, where `fps` is `iteration.checks.fps.value` when numeric, else 60.
- Keys from `usePlayerKeys(handlers, {enabled})`. Single keys, case-insensitive, skipped while `isTyping`:

  | Key | Action |
  |---|---|
  | Space / K | play/pause |
  | ← / → | −5 s / +5 s |
  | J / L | −10 s / +10 s |
  | `,` / `.` | pause, then one frame back / forward |
  | `<` / `>` | speed down / up |
  | `0`–`9` | seek to n×10% |
  | C | comment at now |
  | G | general comment |
  | V | compare versions (Task 6) |
  | F | fullscreen |
  | `?` | key sheet |
  | Esc | close sheet or composer |

- `ScrubBar({duration, current, downloaded: [number, number][], markers: {id, start, end?, kind, active}[], onSeek(s), onRangeSelect(start, end)})`:
  - The `downloaded` byte ranges are drawn as a lighter band, mapped to time by `byte / size * duration` (approximate for variable bitrate; tooltip "Downloaded (approx.)").
  - Click seeks.
  - A plain drag scrubs.
  - Shift+drag, or dragging from the "Range" handle shown while the composer is open, selects a range and calls `onRangeSelect`.
  - Markers render as kind-coloured ticks, or bands for ranges. Each has `aria-label="Fix at 3:42"`, and clicking one seeks.
  - The bar is a `role="slider"` with `aria-valuenow`, and arrow keys while focused delegate to the same ±5 s.
- Start position: `seekS` from the route if present, else `position_s` from `GET /iterations/{id}` when it is under `duration − 5`, else 0. Position is saved with `PUT /iterations/{id}/position` every 10 s while playing, on pause, and on unmount, but never more often than every 10 s.
- `KeySheet` is a dialog listing the key table above. `Esc` or `?` closes it.

- [ ] **Step 1: Write the failing tests.**
  - `timecode` round trips for the examples above.
  - `usePlayerKeys` fires handlers for each key, ignores keys from an `<input>`/`<textarea>`, and `Esc` stops propagation only when something was closed.
  - `ScrubBar`: a click at 25% width calls `onSeek(duration * .25)`; shift-drag from 10% to 20% calls `onRangeSelect`; markers have labels; only non-carried timed comments are passed in (the player's job, asserted in the `ReviewPlayer` test).
  - `ReviewPlayer` with `useMediaFile` mocked: the loading panel shows the GB/percent text; play is offered only at 100% or after **Start watching now**; once ready it seeks to `seekS` over `position_s`; a saved position near the end starts at 0; position PUT is throttled (injected clock); `,` pauses and steps `1/60` when fps is unknown; the speed cycles; `?` opens the sheet; Esc closes it, and the shell's zoom-out does not fire.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the site suite, tsc, lint and build.**
- [ ] **Step 5: Commit** "Editor: review player with keys and resume".

### Task 5: Site: comments in the player, threads, resolve, New badges, unsent queue

**Files:**
- Create: `lib/unsentQueue.ts`, `lib/useComments.ts`, `components/editor/CommentPanel.tsx`, `components/editor/CommentComposer.tsx`, `components/editor/CommentItem.tsx`
- Modify: `components/editor/ReviewPlayer.tsx` (wires the panel, markers, C/G and range select)
- Test: `lib/unsentQueue.test.ts`, `lib/useComments.test.ts`, `components/editor/CommentComposer.test.tsx`, `components/editor/CommentPanel.test.tsx`

**Interfaces:**
- `unsentQueue` stores its entries in `localStorage` under the key `editor.unsent`. An entry is `{client_id, kind: "comment" | "reply", iterationId?, commentId?, payload, created_at}`.
  - `enqueue(entry)`, `remove(client_id)`, `list(): Entry[]`.
  - `flush(send: (e) => Promise<"sent" | "retry" | "drop">)` goes in insertion order and stops at the first `"retry"`, so order is kept.
  - What counts as `"retry"`: a network error, 502/503/504/530, or any 5xx. A 4xx is `"drop"`, with the server's message kept for display in `useComments`' `failed` list.
  - Every access is wrapped in try/catch; with storage unavailable, sending still works and nothing is queued.
  - `client_id` comes from `crypto.randomUUID()` with dashes removed (32 hex characters, which matches Task 1's pattern).
- `useComments(iterationId, {role, username})` returns `{comments, unsent, failed, add(draft), reply(commentId, body), edit(id, patch), setResolved(id, resolved), reload()}`.
  - It loads from `GET /iterations/{id}` and calls `POST /iterations/{id}/seen` once on mount to get `previous_seen_at`.
  - `isNew(item)` is true when `author !== username` and `created_at > (previous_seen_at ?? -Infinity)`; it applies to comments and to replies.
  - `add` and `reply` enqueue first, then flush. Flushing also happens on `online` and every 30 s while there are entries and the page is visible.
  - Sent items replace their placeholder after `reload()`. Unsent ones render from the queue with "Not sent yet" and the time they were written.
- `CommentComposer` opens with C at `start = currentTime` (the video pauses) or with G as general.
  - Fields: kind (Fix/Keep/Question, default Fix), body textarea, start time (editable `m:ss.f`), and an optional end time with **Set end to now**. A range from `ScrubBar.onRangeSelect` fills both.
  - Validation is `end > start`. Submit is Ctrl/Cmd+Enter or the button; Esc cancels.
- `CommentPanel` has filter tabs (Open / All / Resolved). The order is timed comments by `start_s`, then general comments, then carried checkboxes.
  - The active item (`start ≤ t < (end ?? start + 4)`) is highlighted and scrolled into view, smooth unless reduced motion is on, but only while the user has not scrolled the list in the last 5 s.
  - Clicking a timed item seeks.
  - Each item shows the kind chip, time or range, author, body, "New", "Edited", the replies thread with a reply box, and Edit for the author.
  - Resolve is a checkbox for admin and a read-only state for the editor.
  - Carried items show a checkbox and "from v1 at 3:42", a link to the origin route.
  - The panel header shows "3 of 5 resolved".

- [ ] **Step 1: Write the failing tests.**
  - `unsentQueue` survives a reload (a new module instance reading the same `localStorage`); `flush` stops at the first retry and keeps the order; a 4xx drops the entry and reports it; a throwing `localStorage` still sends.
  - `useComments`: an add while fetch rejects shows "Not sent yet", then sends once on `online` with the same `client_id`; `isNew` follows `previous_seen_at` and the author; resolve as editor is not offered, and a 403 if forced is surfaced.
  - `CommentComposer`: C prefill pauses the video; `end ≤ start` is blocked; Ctrl+Enter submits; typing `c`, space or `?` in the body types and does not trigger player keys (Review Focus 4).
  - `CommentPanel`: ordering; the active highlight moves with `currentTime`; a carried item has no marker and links to the origin route `#/p/1/t/2/v/7@222` (the carried-comment rule); the editor sees no resolve checkbox; New badges show on another person's comment or reply after `previous_seen_at` only.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the site suite, tsc, lint and build.**
- [ ] **Step 5: Commit** "Editor: comments in the player with threads and an unsent queue".

### Task 6: Site: compare versions, search, card menu hooks

**Files:**
- Create: `components/editor/SearchBox.tsx`, `lib/useSearch.ts`
- Modify: `components/editor/ReviewPlayer.tsx`, `components/editor/EditorShell.tsx`, `components/editor/IterationCard.tsx`
- Test: `lib/useSearch.test.ts`, `components/editor/SearchBox.test.tsx`, plus additions to `ReviewPlayer.test.tsx` and `IterationCard.test.tsx`

**Interfaces and behaviour:**
- **Compare (V):**
  - The partner is the newest `ready` version of the same tape with a lower version number (from `useTape(tapeId)`). Without one, V shows the toast "No earlier version to compare".
  - Pressing V navigates to the partner's route with `@<currentTime>`. Pressing V there goes back to the newer one at the same moment.
  - `useMediaFile` receives that partner id as `comparePartnerId`, so the first switch downloads it once and later switches are instant.
  - The header shows "Comparing v2 ↔ v3 · V to switch".
- **Search:** a `SearchBox` in the shell's top bar (both roles). Typing `/` focuses it unless already typing. Input is debounced 250 ms, and the minimum is 2 characters.
  - `useSearch(q)` calls `GET /search?q=<encoded>` (the query sits in the URL; it is search text, not a secret) and returns `{hits, loading, error}`.
  - Results are a dropdown of up to 50 hits: "v3 · 3:42 · <snippet>" with Comment/Reply labels. Enter or click goes to `#/p/<project_id>/t/<tape_id>/v/<iteration_id>@<start_s>`, with no `@` when `start_s` is null. Esc closes the dropdown.
- **Card menu:** `IterationCard`'s menu gains **Add comment**, which goes to the version's route and sets a one-shot `sessionStorage` flag `editor.openComposer`, so the player opens the composer at the resume time. Edit checklist is the same panel, so there is no separate item.

- [ ] **Step 1: Write the failing tests.**
  - V goes to the previous ready version at the current time; V with no earlier version shows the toast.
  - The search debounce sends one request for "jet" typed quickly; the result click builds the right hash; a null `start_s` builds no `@`; `/` focuses the box but not while typing in a reply.
  - The card's Add comment sets the flag, and the player opens the composer once, then clears the flag.
- [ ] **Step 2: Run them.** Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the site suite, tsc, lint and build.**
- [ ] **Step 5: Commit** "Editor: compare versions and comment search".

### Task 7: Ship

- [ ] `npx next build` still emits `/`, `/admin`, `/editor`, `/privacy` and `/terms`. The backend suite and the site checks are green.
- [ ] Docs:
  - Roadmap: tick the 5B rows, set the 5B status, and fill "What each plan hands on → Plan 5B" with the module names above.
  - `/home/aryan/projects/CLAUDE.md`: one paragraph on the player (the OPFS cache policy, the token rule, `client_id`, the iteration seen marker).
  - The project memory file `editor-app-project.md`.
- [ ] Push both branches with the user's go-ahead, then open PRs to `stream-backend` and `stream-site`. The site PR depends on the backend PR (`client_id`, `/seen`, CSP).
- [ ] Put a manual smoke list for the streamer in the site PR body. It replaces the riskiest-piece check the spec asks for, since it can only be done on the real machines:
  1. Open a real 1–4 GB version in Chrome and in Firefox. The progress and the ring show, Play is offered at 100%, and seeking is instant. On another version, press **Start watching now** at about 10%: it plays at once, jumping ahead past the downloaded part plays (maybe after a short buffer), and the ring keeps filling.
  2. Close the tab at about 50%, reopen, and the download continues. Open another version of the same project and come back: it is instant. Go to Home, come back, and it downloads again (it was cleared on leaving the project).
  3. Press C, type, send. Stop the Mac Mini backend, write a comment ("Not sent yet"), start it again, and the comment appears exactly once.
  4. On a v2 with an open v1 comment, check the checkbox shows "from v1 at …" and opens v1 there.
  5. Press V to compare. Press `?` for the sheet.

## Not in this plan

- The visual design pass across the editor site (zoom transitions, restyled native `confirm`/`prompt`). It is a separate pass after 5B, agreed with the streamer.
- The 5A follow-ups parked in the roadmap's "Later" section.
