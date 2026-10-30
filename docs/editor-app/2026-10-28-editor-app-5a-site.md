# Editor App Plan 5A: Editor Site (Navigation and Workflow) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The `/editor` page both users work in: zoom navigation (projects › tapes › versions) with a breadcrumb, project/clip/tape/version cards, VOD and final downloads, browser uploads of new versions with automatic checks and a thumbnail, two-step deletes with undo, the bell and VOD-ready popup, Editor Home, Fix list, To-do, Storage, Brand kit link, and the security headers.

**Architecture:** One statically exported page, `app/editor/page.tsx`, holding a client-side hash router (`#/p/12/t/5`). Every call goes to the existing `/api/editor/*` routes through one small fetch wrapper; data lives in `lib/use*.ts` hooks with tests, components stay presentational (the site's rule). Bytes never touch the Mac Mini: uploads PUT straight to the Drive resumable-session URL the backend returns; downloads go to Google's direct download link for the file (`https://drive.usercontent.google.com/download?id=<file id>&export=download`), which uses the person's own Google sign-in, so it works even while the Mac Mini is down; Google shows one "can't scan this file for viruses" confirm page for very large files. Plan 5B adds the review player and comments on top of this shell.

**Tech Stack:** Next.js 16 static export, React, Tailwind (existing tokens in `app/globals.css`: `ink`, `bezel`, `seam`, `phosphor`, `field`, `rec`, `amber`, `dim`, fonts `stencil`/`osd`/`mono`), vitest + Testing Library. Backend: Python aiohttp (`mac mini/Stream`, packages `core/`, `stream/`, `editor/`).

**Spec:** `docs/superpowers/specs/2026-10-24-editor-app-design.md` ("Pages and navigation", "Flow 2", "Flow 3", "Flow 6", "Deletion rule", "Checks on every iteration", "Clips and reels", "Also in this build" security items). Wireframes (layout only): https://claude.ai/artifact/XG2WnQFHr4QEr685HawViy

## Global Constraints

- Static export only (`output: "export"`): no dynamic route segments, no server code. One page `/editor`; navigation state in `location.hash`.
- Both roles use the same page. Admin-only actions (rename project, mark Final / undo Final, start over, Storage page, reset clock) are hidden for the editor **and** still refused by the server (`403 {"error": "Only the streamer can do this."}`); the UI shows that error if it ever comes back.
- `/api/editor/me` → `{username, role}` (`"admin"` | `"editor"`) decides what is shown. Response shapes are whatever `editor/editor_api.py` returns; read it, never guess field names.
- Never put an access token in a URL, browser storage, a log line or an error message. The resumable-session URL is the one exception for storage: it is kept in `localStorage` (it can only upload bytes into that one file, never read anything) so an upload survives the browser being closed; it is deleted when the upload completes or is cancelled, and never logged.
- Checks (spec, 2026-10-28): resolution (tapes 1920x1080, reels 1080x1920; lower = warn), frame rate (>= 60 pass, 30 to < 60 warn, < 30 fail), video bitrate (>= 50 Mbps pass, < 50 fail). Duration shown, not checked. No loudness/peak/length checks. A check that cannot be measured is shown as "unknown", never as pass.
- Thumbnail: JPEG, <= 262144 bytes, sent as base64 in `PUT /api/editor/iterations/{id}/media` with `checks` (<= 16384 bytes JSON).
- Deletes: typing the exact name, then a 1-hour countdown with Undo (`POST /api/editor/deletions` `{kind, target_id, confirm}`, `POST /deletions/{id}/undo`, `GET /deletions`). Kinds `vod`, `tape_files`, `project`.
- Tape statuses: `cutting`, `in_review`, `changes_asked`, `final` (labels "Cutting", "In review", "Changes asked", "Final").
- New UI follows the site's existing look (tokens above, the dashboard's panel style); the wireframes give layout, not design. Accessible basics: real buttons/links, focus visible, `Esc` closes dialogs before it zooms out.
- New `lib` hooks each get a test; components that hold logic get a test. Site suite, `npx tsc --noEmit` and `npm run lint` (0 errors) pass after every task.
- Commits: `-c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com`, files staged by name, session trailers. Site branch `editor-app-plan-5a` in `mac mini/Site`; backend branch `editor-app-plan-5a` in `mac mini/Stream`.
- Backend tests: `uv run --no-project --python 3.14 --with-requirements requirements.txt --with-requirements requirements-dev.txt python -m pytest tests -q -p no:warnings` (12 known tesseract failures). Site: `npx vitest run`.

## Review Focus

1. A large upload interrupted (browser closed, Wi-Fi drop, Mac Mini down): the unfinished upload is listed after reopening, and dropping the same file resumes from Google's offset instead of starting over or creating a second version. Pinned in Task 4.
2. The editor never sees or can trigger a streamer-only action, and a 403 that comes back anyway is shown, not swallowed. Pinned in Tasks 2 and 3.
3. A delete in its undo hour is visible everywhere the target appears (badge with countdown + Undo), and the target's actions that the server would refuse are disabled. Pinned in Task 3.
4. Deep links survive reload and Back: `#/p/12/t/5` reopens that tape; a link to something deleted falls back to the nearest existing level with a message. Pinned in Task 2.
5. Checks on an odd file (no video track, variable frame rate, metadata missing): "unknown", never a false pass, and the upload still completes. Pinned in Task 4.

---

### Task 1: Backend: Home, Fix list, brand kit link, security headers

**Files:** `editor/editor_api.py`, `editor/editor_comments.py` (one query), `core/site_server.py`, tests `tests/test_editor_api.py`, `tests/test_site_server.py` (new).

**Interfaces (produces):**
- `GET /api/editor/home` (both roles) → `{"vods_ready": [project...], "deletes_to_confirm": [project...], "open_tapes": [{tape, project_name, latest_iteration_id, latest_version, open_comments}]}`. `vods_ready`: `vod_status == "ready"` and `download_clicked_at` null. `deletes_to_confirm`: `vod_status == "ready"`, clicked, and no live `vod` delete request. `open_tapes`: tapes whose latest ready version has unresolved comments, most open first. Projects in `project`-delete limbo excluded.
- `GET /api/editor/fix-list` (both roles) → `{"items": [{comment, tape_id, tape_name, project_id, iteration_id, version}]}`: every unresolved comment (including carried-over checkboxes) on each tape's latest ready version, oldest first. Comment text stays in JSON (never in a URL).
- `POST /api/editor/iterations/{id}/session` (both roles) → `{"upload_url"}`: a fresh resumable session for a version still `uploading` (Google expired the old one after ~a week): same tape folder, same `"v<N> - <filename>"` name and `appProperties {"iteration_id"}`, size from the row, the site's Origin; saved as the row's `upload_url`. 409 if the version is not `uploading`; refused like uploads while a delete is live.
- `GET /api/editor/me` adds `"brand_kit_url"`: `https://drive.google.com/drive/folders/<meta gdrive_brand_kit>` or `null`.
- Headers on every response from `site_server` and the API: `X-Content-Type-Options: nosniff`, `Content-Security-Policy: frame-ancestors 'none'` (homepage) and, for `/editor` and `/admin` documents, a full CSP: `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' https://www.googleapis.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`. (`'unsafe-inline'` scripts: Next's static export inlines its bootstrap; a nonce is impossible without a server.)

- [ ] Tests first: a new session replaces the URL and keeps the name/properties, 409 once ready; home lists each bucket correctly and excludes a project being deleted; fix-list shows only latest-version unresolved items and both roles get 200; `me` brand kit url null until the meta is set; `/editor` response carries the CSP and nosniff; `/` carries `frame-ancestors 'none'` and nosniff but not the `/editor` CSP (the homepage loads cdnjs/S3/Twitch/YouTube content). Run, fail; implement (a middleware in `core/server.py` or `site_server` for headers); run the backend suite; commit.

### Task 2: Site: API client, hash router, page shell, breadcrumb

**Files:** `lib/editorApi.ts`, `lib/useHashRoute.ts`, `lib/useEditorMe.ts`, `app/editor/page.tsx`, `components/editor/EditorShell.tsx`, `components/editor/Breadcrumb.tsx`, tests beside each.

**Interfaces:**
- `editorApi<T>(path: string, init?: {method?, body?}) : Promise<T>`: JSON in/out, `credentials: "same-origin"`; non-2xx throws `EditorApiError {status, message}` with the server's `error` text; a 401 sends the browser to `/auth/login?next=/editor` (hash kept).
- `useHashRoute(): {route: Route, go(route: Route): void, up(): void}` where `Route = {view: "projects" | "home" | "fixes" | "storage" | "todo"} | {view: "project", projectId} | {view: "tape", projectId, tapeId} | {view: "iteration", projectId, tapeId, iterationId, seekS?}`; hash forms `#/`, `#/home`, `#/fixes`, `#/storage`, `#/todo`, `#/p/12`, `#/p/12/t/5`, `#/p/12/t/5/v/3`, `#/p/12/t/5/v/3@222.5`. Unknown hash → `projects`. `up()` = one level out; `Esc` and the browser Back both work (hash history).
- `EditorShell`: top bar (site style) with nav: Projects, Home (editor default landing), Fix list, To-do, Storage (admin only), Brand kit (link, new tab, only when `brand_kit_url`), the bell slot (Task 5), and the Transfers panel slot (Task 4). Breadcrumb `All projects › <project> › <tape> › v<N>` with each crumb clickable. Zoom transition: a short scale+fade (CSS, `prefers-reduced-motion` respected) when going in/out.
- A route to something that 404s shows "That was deleted or never existed" and moves to the nearest parent that loads.
- Admin dashboard (`components/admin/AdminDashboard.tsx`) gets a plain "Editor app" link to `/editor`.

- [ ] Tests: router parses/serialises every form, `up()` from each level, Esc ignored while a dialog is open (dialogs stop propagation); api client throws server text, redirects on 401; shell hides Storage for the editor; breadcrumb renders names and navigates; 404 falls back to parent with message. Implement; site suite + tsc + lint; commit.

### Task 3: Site: projects, clips, tapes and versions views; deletes

**Files:** `lib/useProjects.ts`, `lib/useProject.ts`, `lib/useTape.ts`, `lib/useDeletions.ts`, `components/editor/ProjectsView.tsx`, `ProjectView.tsx`, `TapeView.tsx`, `IterationCard.tsx`, `DeleteDialog.tsx`, `PendingDelete.tsx`, `StatusLabel.tsx`, tests.

**Behaviour:**
- **Projects view:** two sections. *VODs*: newest first, card = date, name, VOD state (uploading / ready / deleting / deleted), New badge, tape count; **Download VOD** (ready only): `POST /projects/{id}/download` then start the browser download from Google's direct link built from the project's `vod_file_id` (records the editor's click; the streamer's click is not recorded server-side). A smaller "Open in Drive" link (`drive_url`) sits beside it. Admin: Rename. *Clips*: **oldest first** (spec), card = name, duration, its reel's status, "used in <reel name>" link when combined. Select 2+ clips → **Combine** (name prompt) → `POST /clips/combine`; a combined reel shows **Uncombine** (refused by the server once any version exists; show its message).
- **Project view (VOD):** tapes with status label, latest version, open-comment count, New badge; **New tape** (name); filter by status and sort (newest / most open comments / name).
- **Tape view:** tape name (editable), tape notes (textarea, saved on blur), status select (editor can't pick or leave `final`; admin can, through a confirm dialog "Mark Jett dash Final? You'll download it, then its files are deleted."), version cards newest first. **Final download** (status final): the same direct download link for the latest version's `drive_file_id`, plus "Open in Drive". Admin: **Start over** on the latest version (confirm). Upload drop zone slot (Task 4).
- **IterationCard:** thumbnail (`GET /iterations/{id}/thumbnail` when `has_thumbnail`, else a placeholder), `v<N>`, upload state, check badges (pass/warn/fail/unknown with the measured value in the tooltip), summary line "3 of 5 resolved" (from the iteration's comments; Plan 5B fills in the player), menu (hover; tap on touch): Play (→ `#/p/../t/../v/N`, the player is 5B: until then the iteration view shows details + Download), Change status, Download.
- **Deletes:** one `DeleteDialog` (type the exact name; button disabled until it matches; server error text shown) for VOD (only after the editor's Download click, server-enforced), tape files (Final tapes), project. Live requests (from `useDeletions`, polled every 30 s while the page is visible) show a `PendingDelete` badge with a minute countdown and **Undo** wherever the target is displayed; the target's upload/new-tape/download buttons are disabled meanwhile.

- [ ] Tests (each hook + the logic-bearing components): clips sorted oldest first; combine sends ids + name and shows server refusal; editor sees no Rename/Final/Start over; status select blocks `final` for the editor; delete button enabled only on exact name; pending delete shows countdown + Undo and disables actions; Download calls the API then navigates to the direct download URL for that file id (location mocked). Implement; suite + tsc + lint; commit.

### Task 4: Site: version upload, resume, checks, thumbnail, Transfers panel

**Files:** `lib/resumableUpload.ts`, `lib/mediaChecks.ts`, `lib/useTransfers.ts`, `components/editor/UploadDrop.tsx`, `components/editor/TransfersPanel.tsx`, tests.

**Interfaces:**
- `resumableUpload(file: File, sessionUrl: string, {onProgress(sent, total), signal}) : Promise<{id: string}>`: PUT 8 MiB chunks (`Content-Range: bytes a-b/total`), 308 + `Range` → next offset, 200/201 → file JSON; resume = PUT empty body `Content-Range: bytes */total` first; 404/410 → throws `SessionExpired`; network errors retry with backoff `min(5 * 2 ** n, 300)` s while `navigator.onLine`, pausing while offline.
- Flow (UploadDrop, drag-drop or picker, one file per drop): `POST /tapes/{id}/iterations {filename, size, mime}` → `{iteration_id, version, upload_url}` (507 → "Drive is too full: needs X GB, Y GB free"); remember `{tapeId, name, size, lastModified} → {iteration_id, version, upload_url, sent}` in `localStorage`; upload; `POST /iterations/{id}/complete {file_id}`; then checks + thumbnail → `PUT /iterations/{id}/media`. **After the browser is closed and reopened** (a browser cannot read a local file again without the person choosing it, so this needs one action): the Transfers panel lists every unfinished upload from `localStorage` ("Jett dash v3, 62% sent - drop the file again to continue"), and dropping or picking the same file (same name+size+lastModified) asks Google for its offset and continues from there instead of creating a new version. Google keeps the session about a week; after that (404/410) the panel says so and the next drop gets a new session for that same version (`POST /iterations/{id}/session`) and uploads it from the start. If the upload had actually finished before the close, the server's `reconcile` job registers it and the panel entry clears on its next refresh (the panel checks each stored entry with `GET /iterations/{id}` and drops entries whose version is no longer `uploading`).
- `measureMedia(file: File) : Promise<{width, height, duration_s, fps: number|null, bitrate_mbps: number|null}>` via a hidden `<video>` on an object URL: dimensions + duration from metadata; fps by counting `requestVideoFrameCallback` frames over ~2 s of muted playback (null if unsupported); bitrate = `file.size * 8 / duration / 1e6` (overall; labelled "≈"). `grade(measure, kind: "tape" | "reel") : Checks` applies the Global Constraints thresholds → `{resolution, fps, bitrate: {state: "pass"|"warn"|"fail"|"unknown", value}, duration_s}`.
- Thumbnail: seek to 10% (min 1 s), draw to a canvas at <= 640 px wide, `toBlob("image/jpeg", q)` lowering q until <= 262144 bytes.
- `TransfersPanel`: fixed corner panel listing every upload with name, %, speed, state (uploading / paused offline / waiting for the Mac Mini / done / failed + message), Cancel (abort; the version stays `uploading` and the server abandons it after 8 days). A `beforeunload` prompt while any upload is running.

- [ ] Tests: chunk headers and offset handling (fake fetch, a 20 MiB Blob), resume queries first, SessionExpired on 404/410, offline pause; same file resumes the stored iteration and creates no new one; an entry survives a simulated reload (localStorage) and is shown as unfinished; completion and cancel delete the entry; grade(): 1920x1080 60 fps 55 Mbps tape → all pass; 1280x720 → warn; 29.97 fps → fail; 40 Mbps → fail; fps null → unknown; reel 1080x1920 → pass, tape-sized reel → warn; thumbnail stays under the limit (canvas mocked); 507 message formatting. Implement; suite + tsc + lint; commit.

### Task 5: Site: bell, VOD-ready popup, Home, Fix list, To-do, Storage

**Files:** `lib/useBell.ts`, `lib/useHome.ts`, `lib/useFixList.ts`, `lib/useTodos.ts`, `lib/useStorage.ts`, `components/editor/Bell.tsx`, `VodReadyPopup.tsx`, `HomeView.tsx`, `FixListView.tsx`, `TodoView.tsx`, `StorageView.tsx`, tests.

**Behaviour:**
- **Bell:** `GET /notifications` on open and every 60 s while visible (no `after=`, so the 50-item page never skips events, per Plan 3's note); badge = `unseen`; opening the panel calls `POST /notifications/seen {up_to_id: newest id}`; each item links to its project/tape/version.
- **VOD-ready popup** (editor only): on page load, the newest unseen `vod_ready` items → one dialog "3 new clips are ready" with links; dismiss marks seen.
- **Home** (editor's landing view, also reachable by the streamer): the three lists from `/api/editor/home` with actions inline (Download VOD; Confirm delete → DeleteDialog; open tape).
- **Fix list:** items from `/fix-list` grouped by tape, each "v3 · 03:42 · Fix: <text>" (carried checkboxes show "from v1 at 03:42"); click → iteration route with the start time appended (`#/p/1/t/2/v/3@222.5`; `Route` for `iteration` carries `seekS?: number`) for 5B's player to seek.
- **To-do:** add, tick, edit, delete (`/todos`); private per user (server-enforced).
- **Storage** (admin): Drive free/used, app files with sizes, pending deletes with countdown + Undo, the `scheduler.paused_since` warning with **Trust the clock again** (`POST /scheduler/reset-clock`, confirm), the Discord block (`enabled`, `pending`, `last_error`, `last_sent_at`) and Drive connection state; per-project/tape **Delete** entries open DeleteDialog.

- [ ] Tests: bell badge, seen call with newest id, polling skipped while hidden; popup only for the editor and only unseen vod_ready; home renders each bucket; fix list ordering and seek hash; todos CRUD; storage hidden for editor, reset-clock confirm, discord error shown. Implement; suite + tsc + lint; commit.

### Task 6: Ship

- [ ] `npx next build` produces `/editor` (and still `/`, `/admin`). Manual smoke list in the PR body for the streamer: log in as admin → `/editor`; editor login lands on Home; drop a small video on a tape → progress → version card with thumbnail and badges; mark Final → download → delete with undo.
- [ ] Push both branches (with the user's go-ahead), PRs to `stream-backend` and `stream-site`; update the roadmap rows and "hands on" section, CLAUDE.md (editor site section), and the project memory.

## Not in this plan (Plan 5B)

The review player (whole file into OPFS before playback), comments in the player (C to capture, ranges, general, markers, list following playback, replies/threads UI, resolve and carried checkboxes), player keys and `?` sheet, resume position, compare versions, unsent-comment queue, search UI.
