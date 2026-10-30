# Site fixer context
- Repo /home/aryan/projects/mac mini/Site, branch `later-fixes` (checked out). Commit there; never push.
- Read /home/aryan/projects/CLAUDE.md frontend + editor sections and the plan /home/aryan/projects/docs/superpowers/plans/2026-10-29-editor-app-5b-player.md Global Constraints first.
- Rules: data in lib/use*.ts hooks each with a test; logic-bearing components tested; injected clocks for timing; access token never in URL/storage/log/error; user text as text only; storage access in try/catch; keys typed in inputs ignored by the player (isTyping); Esc order composer > sheet > shell.
- Checks after all changes: `npx vitest run`, `node_modules/.bin/tsc --noEmit`, `npm run lint` (0 errors), `npx next build` (if Turbopack "Failed to open database": rm -rf .next out). Do not commit tsconfig.tsbuildinfo.
- Each fix gets a test that fails without it (show RED then GREEN in the report).
- Commit: `git -c user.name=aryanverma7 -c user.email=ultimateshedic@gmail.com commit`, stage BY NAME (Zone.Identifier junk), message ends with:
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EBDRBtUCJgM7PK9fWR7XzN
- No subagents. Safest option on undecided details; note it in the report.
- Reply with ONLY status, commits, one-line test summary, concerns, report path.
