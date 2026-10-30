# Site "Later" batch (streamer: finish everything under Later). Repo mac mini/Site, branch later-fixes. Each item gets a test that fails without it.

5A leftovers
1. VodReadyPopup Esc test flaky: make the Esc handler registration deterministic (useLayoutEffect or register before paint) and the test robust; run it 20 times in a loop to show it's stable.
2. Cancel during "waiting for Mac Mini" orphans the finished file: during `waiting` the bytes are already on Google, so Cancel is replaced by "Finishing…" (no abandon); if the person still wants it gone, they delete the version normally. Test.
3. Combine selection: a clip with a live delete can't be selected (disabled with "Being deleted") and isn't counted. Test.
4. Home: hide projects in project-delete limbo (use useDeletions) if the backend still returns them. Test.
5. Native confirm/prompt dialogs: replace every window.confirm/window.prompt in components/editor (and admin if any) with one accessible in-site dialog component (ConfirmDialog/PromptDialog: focus trap, Esc closes, site tokens). Tests.

5B leftovers
6. V while playing auto-starts the partner only when the partner is fully downloaded; otherwise it opens paused with Play/Start watching now. Test.
7. Shift+reload: when the page has a registration but no controller, post {type:"claim"} to registration.active; media-sw.js calls self.clients.claim() on that message; useMediaWorker becomes ready on controllerchange. Keep the reload hint as fallback. Test (fake serviceWorker).
8. media-sw.js need-link waiters: resolve a waiter only with an answer that satisfies its need (a "link" waiter ignores link:null answers and keeps waiting until timeout). Test in lib/mediaSw.test.ts.
9. New badges count edits: a comment by someone else whose updated_at > previous_seen_at (and != created_at) shows "Edited" + New. Test.
10. SearchBox: ArrowUp/ArrowDown move an active option (aria-activedescendant on the input, role=option aria-selected on the active one), Enter picks the active one, Home/End. Test.

Audit leftovers
11. Unsent entries saved before the username field: show them in the comment panel as "Unsent from before an update" with "Send as me" / "Discard". Test.
12. Checks/thumbnail missing (tab closed during checking, or reconcile completed the upload): a ready version with no checks shows "Run checks" which asks the person to pick the file (same name+size) and runs measureMedia + thumbnail → PUT /media. Test.
13. A second tab can cancel another tab's upload: each running upload holds a Web Lock `upload-<iterationId>`; a tab that sees the lock held shows the entry as "Uploading in another tab" without Cancel. Test with fake locks.

Do NOT change the fps grading (streamer is deciding).
Report: /home/aryan/.claude/jobs/48c13ad5/tmp/5b-audit/later-site-report.md
