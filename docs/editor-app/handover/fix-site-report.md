# Site fix report (whole-codebase audit)

Repo `/home/aryan/projects/mac mini/Site`, branch `audit-fixes`, not pushed.

## Commits
- `1a4b8c9` Editor player: audit fixes (flags, locks, seen, retries, keys): items 1-18, plus IterationCard (#19's Cancel lives in the same file as K1's flag value)
- `c5b4f81` Site: audit fixes (uploads, settings, status, chat, tape notes): items 20-32

## Checks (final, after all changes)
- `npx vitest run`: 75 files, 554 tests, all pass (was 72 / 506)
- `node_modules/.bin/tsc --noEmit`: clean
- `npm run lint`: 0 errors, 4 warnings (all from before this work)
- `npx next build` (after `rm -rf .next out`): OK, routes / /_not-found /admin /editor /privacy /terms
- `tsconfig.tsbuildinfo` not committed. Files were staged by name.

## RED then GREEN
Method: the fixed source files were reverted to HEAD (tests kept) and the suite was run, then the sources were put back. 42 tests failed against the old code and all pass now. Failing names (RED):
- ReviewPlayer: V while playing (flag value `5:<ms>`); flag for another version / resume older than 30 s; failed load clears flags + h2 + Try again; leaving while loading drops the flag; storage-only Try again reloads the video (made stricter: the old code passed it by remounting the whole player, so it now asserts the video element survives); reloaded video shows Play and keeps the speed; range select in fullscreen leaves fullscreen and pauses; comments shown during the download with seek disabled, /seen posted once across Try again; "Waiting for the Mac Mini / Drive: ..."; holds shared locks
- IterationCard: flag holds the id; Cancel this upload (confirm, abandon, refresh)
- KeySheet focus in, trapped, and returned; ScrubBar markers outside the slider; SearchBox blurs on pick and closes on blur (2 tests)
- opfsMedia: 403 rate limit retried; five short chunks fatal; no folder once aborted; freeSpaceFor and keepOnlyProject skip another tab's lock. The "final media-link answer is fatal" test ran the old code out of memory (endless retries, which is the bug), so its RED is that OOM.
- useMediaFile: final media-link answer ends the download; retrying message; no folder after leaving. useMediaWorker: need-link for a downloaded version answers at once
- mediaSw: cancel aborts the in-flight Drive fetch
- unsentQueue: only the signed-in user's entries. useComments: placeholder kept until the reload lands; another user's entries and a 409 client_id
- playerLogic: .mov served as video/mp4. useHashRoute: seekS to 3 decimals
- useTransfers: cancel keeps an abandoned entry; re-drop revives it via /session; 409 falls back to a new version; /session retried while the Mac Mini is down. A failed abandon never undoes the cancel (assertion updated)
- TransfersPanel: prompts on close for offline and retrying
- TapeView: debounce save (injected delay); save on unmount
- ConfigEditor: jump to an unset secret's Replace; list entry added on blur. useConfigEditor: reload failure is not a save failure
- StatusPanel: no Tesseract warning when tesseract_available is absent; no "All feeds OK" when no feed is up
- useBreakTimer: hidden and busy polls; MergedChat reconnects with backoff; LegalPage plain link

## Choices made where the task left details open (the safer option in each case)
- K1: the `openComposer` value is the bare id, as specified. ReviewPlayer drops flags for other ids when the id changes, drops all flags on a load error, and drops its own id's flags on unmount. The unmount drop waits a microtask, so StrictMode's unmount and remount keeps them.
- I1: what this page holds itself is counted and subtracted (`mine`), so a tab's own just-unmounted Watch never stops its own route-change cleanup. Only other tabs' locks protect. If the lock query fails, nothing is treated as held.
- I2: /seen is memoised in ReviewPlayer per iterationId and passed into useComments as `seen`; the default POST is unchanged for other callers. A rejected /seen is also kept, so it shows nothing as new and is not re-POSTed.
- I3: the onRetry message is the error's own text (for example the server's 503 text, or "Drive error 502"). Every short chunk counts toward the limit of 5; the count resets for each chunk.
- M4: reload() now refreshes the placeholders, and only when it succeeds. If the reload fails, a sent entry's placeholder stays until the next successful reload.
- M5: an `editor.unsent` entry without a username (written before this change) is never sent or shown. That is safer than posting it under whoever is signed in. Any such leftover entries just sit in localStorage.
- M6: one AbortController per response, which covers each Drive fetch made for it. An error body that is not used is cancelled.
- M14: the sheet's only control is Close, so Tab and Shift+Tab keep focus on it. The marker buttons sit in a relative wrapper next to the slider, at the same positions as before.
- #2: a cancel during the version create still stores nothing (the existing test). Abandoned entries stay hidden in localStorage until the same file is dropped again, or until refreshStored sees the version is gone or finished.
- #3: while /session is retried, the transfer shows "retrying" rather than "waiting" (the bytes are not on Google yet).
- #4: "waiting" still does not ask before closing (an existing test says so: the bytes are on Google and reconcile finishes the version). Only offline and retrying were added.
- #9: the Replace button has `aria-label="Replace…"`. Without it, the field's `<label htmlFor>` would rename the button.
- #12: when no feed is up, the summary says "Nothing reporting yet" with the "huh" sticker. `Summary` is exported so it can be tested, because a live status always has Streamer.bot up or down.
- #19: the card's Cancel is hidden while this tab has a transfer for that tape and version (sending or paused). The Transfers panel has its own Cancel, and cancelling server-side while this tab keeps sending would be undone by /complete.
- #16: I did not have the audit's reason for the plain `<a>`. The comment in the code says only that the homepage loads as a whole document. The lint rule `no-html-link-for-pages` is disabled on that one line.

## Concerns
- The media-sw vm test checks range planning across disk and Drive, 416, the need-link timeout giving 503, the 401 retry, and cancel aborting the fetch. Removing a timed-out waiter has no test of its own.
- The backend changes were assumed as described (media-link 404/409 text, 409 "client_id already used", download `{ok:true}`). Nothing on the site read a token from /download.
- The 4 lint warnings are from before this work (layout font, two location.assign, StatusPanel unused `index`).

## Follow-ups from review (commit below)
1. media-sw.js sends `need: "info"` for the info-only ask in serve(), and `need: "link"` from info() (a Drive piece is needed, or a forced refresh after a 401). useMediaWorker answers link:null at once only for `need: "info"` on a downloaded version. Every other ask waits for a real link.
2. useTransfers revive: a 404 from POST /session is handled like 409 (the entry is dropped and a new version created). A cancel while /session is in flight calls abandon(iterationId) again, both when /session succeeds and when it throws after the abort, and the stored entry stays abandoned with its old URL.
3. downloadToOpfs calls onRetry(null) as soon as a retried request succeeds, before the chunk is written. useMediaFile's setRetrying(null) then clears the message.
4. MergedChat resets the backoff in onopen. The reset on message was dropped, since a message implies the socket is open.

RED (sources at the previous commit, tests new): 7 failed. These were the two mediaSw `need` assertions, the useMediaWorker need:"link" test, the 404 revive, the cancel during /session, the retry message clearing, and the backoff reset on open. GREEN: 75 files / 559 tests pass. tsc is clean, lint has 0 errors (4 old warnings), next build OK.
