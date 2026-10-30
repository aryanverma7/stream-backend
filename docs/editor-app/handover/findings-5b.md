# 5B player audit
K1 (known) ReviewPlayer.tsx:88-97 editor.resumePlaying AND editor.openComposer linger if Watch never mounts (GET /iterations fails or person leaves while loading). Make the value target-specific ("<iterationId>") and clear both in ReviewPlayer whenever iterationId changes or on load error.
K2 (known) ReviewPlayer.tsx ~361 storage-only + video error: Try again condition treats storage note's media.error as real error -> use reloadVideo: `videoError && !(media.error && !storageOnly) && !worker.error`.
I1 opfsMedia.ts:69-79 keepOnlyProject + EditorShell.tsx:96: a second tab (other project / Home) deletes the first tab's playing/downloading version (OPFS is shared). Fix: each mounted Watch holds a shared Web Lock `opfs-project-<id>` (and per version for freeSpaceFor, e.g. `opfs-version-<dir>`); keepOnlyProject/freeSpaceFor skip held ones via navigator.locks.query(); no locks API -> behave as today.
I2 useComments.ts:53-58 POST /seen on Watch mount, but CommentPanel only renders after started; leaving during download marks everything seen; Try again re-POSTs. Fix: POST /seen once per version view, the first time the panel renders; better: render the comment panel while the version downloads too (editor can read fix notes before playback).
I3 opfsMedia.ts:160-190 retries getLink failures forever: backend media_link 502 on Drive 404 / token refresh fail, 503 not connected, 400 no file -> silent "0%" forever. Fix: backend media_link answers 404 "This file is no longer on Drive" when get_file 404s (and refuse unless status ready/deleting — editor backend M6); site treats EditorApiError 400/403/404/409 as fatal; add onRetry status so the panel shows "Waiting for the Mac Mini / Drive: <message>"; short-chunk loop also bounded/reported.
M4 useComments.ts:90-94 sent placeholder removed before reload returns.
M5 client_id dedupe unscoped (backend author/iteration/comment) and editor.unsent survives logout (posted under the next user's name). Store username per entry; backend lookup WHERE client_id=? AND author=? (+ iteration/comment) else 409.
M6 media-sw.js driveStream no AbortController; cancel race orphans Drive body; timed-out waiters leak.
M7 useMediaWorker.ts:54-58 need-link for a downloaded version waits on getLink(true); answer link:null at once for downloaded versions.
M8 useMediaFile.ts:47-58 no abort check between versionDirs and downloadToOpfs (empty folder recreated after leaving project).
M9 opfsMedia.ts:174 Drive 403 rateLimit/downloadQuota treated as gone; read reason, retry rate limits.
M10 ReviewPlayer.tsx:184-190 reloadVideo leaves paused/speed state stale; reset paused, reapply playbackRate on loadedmetadata.
M11 shift-drag range in fullscreen opens invisible composer; leave fullscreen + pause in onRangeSelect.
M12 SearchBox: focus stays in input after pick (player keys dead); dropdown doesn't close on outside click. Blur in pick(), close on blur.
M13 ReviewPlayer.tsx:70 failed GET /iterations: no Try again, no h2.
M14 KeySheet aria-modal without focus move/trap; marker buttons inside role=slider unreachable.
M15 unverified: mimeOf video/quicktime for .mov may not play in Firefox via SW; serve .mov as video/mp4.
M16 tests: media-sw.js serve/ask/driveStream untested; useMediaWorker.test.ts:50 async assertion can't fail; CommentComposer harness lacks container.
