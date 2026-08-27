# Demo capture, round 2: terminal to dashboard candidates

Supersedes the round 1 candidates in `docs/internal/demo-takes.md`
(the "tools"/"system"/"tool_result" dashboard-only, single-block-expand
takes). Two rounds of user feedback led here:

1. The shipped "tool_result" take rushed the bar fill and only opened
   one block; the user wanted the breakdown itself to get more screen
   time, more than one block opened (including the user prompt), and
   the fake cursor removed entirely.
2. A further pivot asked for a stronger hook: open on a terminal mockup
   typing a real Claude Code style prompt, tab switch into the live
   dashboard, land on the Context Window Explorer for that same prompt.
   Aimed at LinkedIn/README shareability, not just correct information
   disclosure.

`make demo-candidates` renders one take per reveal mode into the
gitignored `docs/_scratch/` (`.mp4` and `.gif` each). Nothing here is
shipped; `docs/demo.mp4`/`docs/demo.gif` are untouched and still the
round 1 "tool_result" take until the user chooses a replacement.

## Structure common to all four

Every candidate is two acts recorded as one continuous Playwright
session, with a real Chromium tab switch between them (`Page.bring_to_
front`, the same primitive an actual Cmd+Tab/Ctrl+Tab triggers), not a
CSS transition standing in for one:

1. **Terminal act** (`mcp_server/demo_static/demo_terminal.html`): a
   scripted terminal mockup types out a sample prompt letter by letter,
   then shows a one line "investigating" response. Not a real shelled-
   out terminal: that would depend on live model output and non-
   deterministic timing, which is exactly what the seeded-database
   approach elsewhere in this pipeline exists to avoid.
2. **Dashboard act**: the real, live dashboard, session already
   selected, landing on the Context Window Explorer for
   `demo-session-12` (the seeded "do a full multi-signal investigation
   of the checkout-api incident" session, or a close variant of that
   prompt per candidate, see below).

No fake cursor anywhere. The tab switch is a real browser event, and
every state change inside the dashboard act (bar fill, KPI count-up,
block row expand) is driven by `demo_reveal.js` as a scripted DOM change
rather than a simulated click, since there is no cursor for a click to
originate from any more.

Total runtime per candidate is roughly 12.5-14s (terminal act, then the
dashboard act's own reveal-mode-specific hold, see
`scripts/demo_capture.py`'s `REVEAL_MODE_HOLD_MS`), inside the ~10-15s
target: enough room for a typing beat, a tab switch and a proper
breakdown reveal without turning into a walkthrough video.

## The four candidates

- **guided_tour** (`docs/_scratch/guided_tour.mp4`/`.gif`): the bar
  fills over 4.2s (the majority of the dashboard act, deliberately, per
  the "breakdown is the product" feedback), settles, then three
  different block kinds open in sequence, each briefly glow-highlighted
  and held before the next: User prompt, then Tool result: list_
  services, then Reasoning (turn 1), which stays open as the final
  frame. This is the direct answer to "open more than one block,
  including the user prompt, to show real visibility": a first-time
  viewer sees three structurally different kinds of context, not one
  cherry-picked example.

- **cost_reveal** (`docs/_scratch/cost_reveal.mp4`/`.gif`): reverses the
  brief's own bar-first ordering. The KPI tiles (Tokens, Cache hit,
  Cost) count up FIRST, before the bar exists at all, then the bar
  fills as the visual answer to "why does it cost this", then Tool
  specs opens with an explicit callout badge ("5 tool schemas, sent
  every turn") rather than a bare expand. Uses a shorter, punchier
  sample prompt ("why did checkout-api page me last night?") since this
  is the most numbers-first take and benefits from getting to the KPIs
  quickly.

- **surprise** (`docs/_scratch/surprise.mp4`/`.gif`): closest in spirit
  to the round 1 "tools" take (Tool specs is still the surprising
  reveal), but given meaningfully more room: a full bar fill, a longer
  settle beat so the completed breakdown registers on its own before
  the twist, then the same block opens with a callout badge ("Tool
  specs: bigger than the user prompt") making the point explicit rather
  than relying on the viewer to notice the token count unaided.

- **multi_turn** (`docs/_scratch/multi_turn.mp4`/`.gif`): groups the
  existing flat block list by its real `turn_n` boundaries (no schema
  change needed, the data is already there) to show context
  accumulating rather than a single static snapshot. The User prompt
  row opens and STAYS open while Tool result: list_services also opens
  alongside it, both visible together on the final frame, a genuinely
  full JSON payload including the incident-tying "note" field. This is
  the strongest single frame of the four: two different real blocks
  fully visible at once plus the completed bar.

## Why these four are structurally different

Not four parameter variations of "fill bar, expand one thing": guided_
tour opens three blocks in sequence and closes each before the next;
cost_reveal and surprise both open one block but differ in what leads
(numbers vs bar) and how the callout is framed; multi_turn opens two
blocks and deliberately leaves both open together rather than cycling
through them. Between them they cover every concrete direction asked
for: multi-block tour, numbers-first reordering, an emphasised single
surprise, and turn-to-turn accumulation.

## Bugs found and fixed while building these

Two real bugs surfaced only once actually recorded and inspected frame
by frame, not visible from reading the choreography code alone:

1. All four tabs (Overview, Context Explorer, Tools, Breakdown) render
   into the DOM at session-select time, not on first click of that tab
   (see `renderSessionDetail` in `auth.py`). `demo_reveal.js`'s reveal
   was starting, running, and finishing on the hidden Context Explorer
   tab before the capture script ever clicked over to it, so every
   scripted block-open had already happened (and, depending on the
   mode, already closed again) by the time the tab became visible.
   Fixed by having the reveal wait for the tab's own `.active` class,
   not just the block rows' existence in the DOM.
2. The block row stagger (180ms per row) was gating how long the fill
   step took to report itself "done". `demo-session-12` renders around
   45 block rows (15 turns), so 180ms/row ran nearly 8s past the
   intended ~4s fill window, silently pushing every later scripted step
   (block opens, KPI count-up) back by the same amount and blowing the
   runtime budget. Fixed by capping the per-row stagger so it always
   fits inside the fill window, regardless of how many rows a given
   session has.
3. Playwright's `recordVideo` keeps writing a page's video for as long
   as that page stays open, not just while it is the visible tab. An
   earlier version of `scripts/demo_capture.py` left the terminal page
   open for the whole recording, producing a ~14s terminal clip for
   what should have been a ~3-5s beat. Fixed by closing the terminal
   page immediately after the tab switch.

## Reused from round 1

The reveal-mode architecture (`REVEAL_MODES` in `demo_reveal.js`, the
staged bar fill, the row stagger, the KPI count-up) is the same
machinery introduced for the dashboard-only round 1 takes, extended
with a mode registry instead of a single hardcoded choreography plus a
glow/callout visual language to replace click-simulation now that there
is no cursor. The zoom-to-block-list CSS transform from round 1 is also
kept, applied once the dashboard act's context tab is active.

## Round 3: guided_tour chosen, split into two cuts

After reviewing all four candidates above, the user picked guided_tour
outright: "guided tour was the best one but it needed a few more
seconds in between user prompt, tool call and reasoning to show to the
user so they can read it, and make it slightly more natural the
movement." cost_reveal, surprise and multi_turn are no longer being
iterated on; their `docs/_scratch/*.mp4`/`.gif` files and their
`REVEAL_MODES` entries in `demo_reveal.js` are left in place (still
reachable via `make demo-candidates` for reference) rather than ripped
out, since none of it blocks guided_tour shipping as the sole concept.

Two changes came out of that feedback:

1. **More read time between beats.** guided_tour's three block-open
   holds went from 1100/1100/900ms to 2600/2600/2200ms for the full cut
   (roughly 2.3x), specifically so a LinkedIn viewer can actually read
   the User prompt, Tool result and Reasoning content before the next
   block opens, per the user's explicit ask. See
   `scripts/demo_capture.py`'s `REVEAL_MODE_HOLD_MS` and
   `demo_reveal.js`'s `guided_tour` mode, both of which must move
   together since the Python hold time has to outlast the JS
   choreography it is holding for.
2. **More natural motion.** The bar-fill width transition, block row
   fade/slide-in, glow pulse and callout fade in `demo_reveal.js`, plus
   the zoom-to-block-list transform in `demo_capture.py`, all moved from
   plain `ease-out` (or, for the zoom, `ease-in-out`) to
   `cubic-bezier(0.16, 1, 0.3, 1)`: a curve that decelerates further
   into the motion, closer to how real UI settles into place, rather
   than the more mechanical, constant-feeling motion `ease`/`ease-out`
   produces by comparison. `ease-in-out`'s slow-start was the most
   noticeably mechanical of the lot (a settle-into-place should only
   decelerate, not also visibly accelerate out of rest first), so that
   one in particular changed.

guided_tour also grew a `cut` parameter (`full`/`short`) instead of
being cloned into a second script, since the user separately asked for
"maybe the shorter version for the gif on the readme": a trimmed edit
of the same choreography, not a different concept. `full` keeps all
three block-open beats with the read-time above and targets the ~15-20s
LinkedIn video; `short` drops the Reasoning beat (keeping the two most
illustrative ones, User prompt and Tool result), shortens the bar fill
slightly, and gives each remaining beat less hold time, targeting the
~8-12s README gif. `scripts/demo_candidate_params.sh` and the Makefile's
`demo` target both take this as `CUT` (default `full`); `make demo
CUT=short` is what rendered `docs/demo.gif`.

Actual rendered lengths: `docs/demo.mp4` (full cut) is 18.3s, `docs/demo.gif`
(short cut) is 11.5s, both within their targets and both replacing the
old "tool_result" take referenced in `docs/internal/demo-takes.md`,
which is now superseded by this decision.
