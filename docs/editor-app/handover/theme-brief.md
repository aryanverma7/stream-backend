# Stream widgets theme brief

Repo: aryanverma7/stream-widgets (private). You work in a git worktree (path in your dispatch) on branch `theme/<name>`. Commit there; never push, never touch `main` or the other theme branches.

Restyle EVERY widget/scene in the repo to one coherent theme:
`break-screen/`, `starting-soon/`, `roulette-overlay/`, `forced-buy-badge/`, `horizontal-chat/`, `spotify-now-playing/`, `rank-session-tracker/`.
Read README.md and each widget's html/css/js first, plus /home/aryan/projects/CLAUDE.md section "`widgets/` splits into backend-fed and self-contained" (deliberate behaviours you must keep).

Hard rules
- Behaviour stays identical: same backend messages handled, same logic, same timings, same tests. Change markup/CSS/visual JS (drawing, animation) only. `./run-tests.sh` must pass at the end (update a test only where it pins a pure visual detail you changed, and say so).
- Chat text and any backend/user string go in via textContent only — never innerHTML with dynamic data.
- Keep each overlay's resize model: the fixed reference size + CSS transform scale (REFERENCE_WIDTH/HEIGHT in .js must match #scale-stage in .css; change both together if you change size). Full-screen scenes are 1920×1080.
- Fonts via Google Fonts <link> only (no other network assets except those already used); everything else inline/local. No external images; draw with CSS/SVG/canvas.
- Legible on stream: text readable at 1080p and when OBS scales the overlay down; chat and timers high contrast. Respect prefers-reduced-motion (calmer, no flashing). No seizure-risk flashing (≤3 flashes/s).
- Rank tracker: config.js is git-ignored; never create or print it. For local preview, copy config.example.js to config.js in your worktree only if you need it and never commit it.
- Commit with `git -c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com commit`, files by name, message ending with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EBDRBtUCJgM7PK9fWR7XzN
- Add `THEME.md` at the repo root: the theme's idea, palette, fonts, motion rules, and one line per widget saying what changed.
- Verify visually: render each html in headless Chromium (playwright; `npx playwright` may need `npx playwright install chromium` — fine) at its OBS size with a stubbed WebSocket feeding sample messages (roulette_started with weapons, weight_updated, roulette_ended; chat_message with emotes; now_playing; break_state; forced_buy_*), and save PNG screenshots to `screenshots/<widget>.png` in the worktree (commit them: the streamer compares branches by these). Look at every screenshot yourself and fix what looks wrong before finishing.
- No subagents.

Reply with ONLY: status, commits, the screenshot paths, one-line test summary, concerns.
