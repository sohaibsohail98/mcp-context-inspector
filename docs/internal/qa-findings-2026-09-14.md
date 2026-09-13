# QA findings — 2026-09-14 (pre-demo pass, ctxwindow.uk)

Found via two ego-browser QA passes (unauthenticated, then authenticated) the night before a demo.
Not acted on yet — logged here for a follow-up fix pass on a separate branch.

## 1. System-reminder misclassification (BLOCKER, root-caused)

A `<system-reminder>` block is classified `injected` correctly only when it arrives as its own
standalone content block. When bundled with real user/assistant text inside the same logical turn,
the classifier assigns the category from the *enclosing message's role* instead of inspecting each
split sub-block independently — so it gets mislabeled `user` or `answer`.

Reproduced 100% of the time across three sessions (21, 392, 430 turns). Example (21-turn session,
"Attribution for git commits…", Turn 0):

| idx | content | category (actual) | should be |
|---|---|---|---|
| 4 | `<system-reminder>` userEmail | `injected` (correct) | injected |
| 5 | `<system-reminder>` git commit/PR attribution | `user` (wrong) | injected |
| 6 | `<local-command-caveat>` | `user` (wrong) | injected/system |
| 7 | `<command-name>/clear</command-name>` | `user` (wrong) | injected/system |
| 8 | `<local-command-stdout></local-command-stdout>` | `user` (wrong) | injected/system |
| 9 | "Transfer the got folder…" (real user text) | `user` (correct) | user |
| 10 | `<system-reminder>` Environment block | `answer` (wrong) | injected |
| 11 | `<system-reminder>` model info + tool list | `injected` (correct) | injected |
| 41, 72, 77 | `<system-reminder>The user sent a new message while you were...` | `answer` (wrong) | injected |

Confirmed server-side, not a frontend bug: `/api/context-timeline/{id}` returns the wrong `category`
field directly; the dashboard's `renderBlockSequence`/`renderCtxBlocks` just render whatever the API
sends.

Downstream effect: the top summary bar's `injected` count is under-reported app-wide because
misclassified reminders get bucketed into `user`/`answer` instead.

**Repro:** open any "Attribution for git commits and pull reque…" session → Context Explorer tab →
expand Turn 0 → the git-attribution `<system-reminder>` block shows the `cat-user` dot and "User
message" label.

## 2. Session titles show raw `<system-reminder>` XML (BLOCKER, likely same root cause)

Many sessions in the list show the literal truncated `<system-reminder>...` tag text as their title
instead of a real derived title — e.g. "Attribution for git commits and pull reque..." is the
attribution reminder text, not a generated summary of the actual user prompt. Likely the title
extraction picks up the same wrong block as bug #1.

## 3. Tool call `args` are always empty `{}` (BLOCKER)

Every tool call row in the Tools tab, across every session checked (21, 6, 430 turns), shows empty
args. `renderToolsTab` does `JSON.stringify(c.args || {})` — `c.args` itself is empty/falsy from the
backend trace data, this is not a rendering bug.

## 4. MCP tool names collapse to generic `mcp_tool` (BLOCKER)

Non-Bash/ToolSearch/Read tool calls show as literally `"mcp_tool"` rather than the real MCP tool
name (356 of 430 calls in one session). Flattens the "Tool reliability" breakdown into one bucket for
anything MCP-based.

## Rough edges (not blockers)

- "5h usage window" / "7d usage window" cards show "—" / "PENDING DATA SOURCE" — appears to be an
  intentionally unwired placeholder, not a regression.
- Breakdown tab: "Spend by subagent/skill", "MCP server connections", "Reliability signals/API
  errors" all say "Not tracked yet." — same, likely intentional placeholder.
- Largest sessions' `/api/context-timeline/...` payload up to 364KB, 2-8s load time — fine
  functionally, just worth knowing for demo pacing if presenting a big session live.
- Unauthenticated pass (separate run): a scroll position on the landing/login page produced a fully
  black screenshot in the QA tool despite the DOM/computed styles looking correct — likely a
  screenshot-tool/compositor artifact rather than a real bug, but never independently confirmed by a
  human. Worth a 30-second manual scroll-through before presenting.

## Suggested next step

New branch off `main`, work bugs #1-#4 together since #1/#2 likely share a root cause (whatever
splits/labels blocks server-side isn't looking at sub-block tags independently), and #3/#4 likely
share a root cause too (the tool-call trace capture, not the display layer). Do not touch this branch
while `terraform/` and CI work from the same night are in flight on `main`.
