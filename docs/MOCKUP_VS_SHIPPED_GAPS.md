# Mockup vs. shipped: the real gaps

The approved design mockup for the dashboard (a Claude.ai artifact,
"Context Window Explorer") and the shipped implementation
(`mcp_server/server.py`'s dashboard-rendering functions) match closely on
overall structure — topbar, KPI strip position, the four detail tabs,
Context Explorer bar/legend/block-list, Tool calls table. This doc lists the
places they diverge, split into two categories that matter differently:

- **Documented deferrals** — the project's own build plan
  (`docs/internal/OTLP_INTEGRATION_PLAN.md`) explicitly marks these as out
  of scope for v1. Shipping without them was the plan, not an oversight.
- **Real gaps** — either the plan required these for v1 and they weren't
  built, or the plan never mentions them at all. These are the honest
  to-do list, in priority order below.

Found via a dedicated fresh-eyes comparison, then independently re-verified
by a second fresh-eyes agent against both the mockup and the actual
rendering code — one item (insight cards) was originally misclassified as a
documented deferral and corrected after that second pass.

## Real gaps, in priority order

### 1. Insight cards — highest priority; this is a plan requirement, not polish

**Mockup:** 3 cards with specific, data-backed suggestions (e.g. "You
typically cross 80% context by turn 7").
**Shipped:** absent entirely.
**Why this ranks first:** the build plan explicitly lists "2-3 example
personalized insights... rendered above or alongside the session list"
among items called "**agreed requirements, not optional polish**" for the
v1 dashboard. Only the *full 30-item catalogue* of possible insight types
(a separate section of the plan) is marked "the backlog, not a v1
requirement." A minimal 2-3 card version was owed for v1 and was never
built — this is scope that shipped incomplete, not a deferred nice-to-have.

### 2. Session list rows show the wrong metric

**Mockup:** source badge, prompt, `"Xm ago · N turns"`, and a colored
context-pressure dot (warn/error states) — an *inline* pressure indicator,
specifically called out in the plan as needing to be visible "directly in
the row — not hidden behind a click, since catching pressure before it
bites is the whole point of the Explorer."
**Shipped:** source badge, prompt, `"Xm ago"`, then `tokens · cost` —
a different metric entirely, and no context-pressure indicator anywhere in
the row.
**Why this matters:** this isn't a missing decoration, it's the plan's own
stated *point* of the feature (surfacing context pressure before it bites)
not being surfaced where the plan says it must be.

### 3. KPI strip — half the tiles are stubs

**Mockup:** 6 populated tiles with deltas and a token sparkline (Sessions,
Tokens, Spend, Cache hit rate, Tool error rate, Context alerts).
**Shipped:** only Sessions / Tokens / Spend are real. Cache hit rate / Tool
error rate / Context alerts render `"—, per-session only"`. No deltas, no
sparkline anywhere.
**Context:** the code comment explains why (computing those three
aggregates would need a per-session detail fetch for every session in the
list — an N+1 query pattern the author deliberately avoided). That's a
reasonable engineering tradeoff, but the plan lists all 6 tiles as an
"agreed requirement," so the tradeoff still leaves a real, visible gap from
what was promised.

### 4. Missing source-filter chip row

**Mockup:** a filter row above the session list — `All sources / Claude
Code / Copilot / Bedrock agent` chips, scoping which sessions are listed.
**Shipped:** absent. (Note: this is different from the *time-range* filter
— Today/7d/30d/All time — which does exist and does match the mockup.)
**Why this is unranked relative to the others:** the plan doesn't mention a
source-filter row anywhere — it specifies per-row source *badges* (which
exist) but never requires a separate filter control. This is a real,
visible mismatch from the mockup, but not a case of shipped-scope-missing
the way items 1–3 are; it's closer to a mockup detail that never made it
into the written requirements at all.

## Confirmed as genuinely out of scope for v1 (not gaps)

| Element | What the plan actually says |
|---|---|
| 5-hour / 7-day usage windows | No officially supported data source exists; the plan's own verdict is to ship it "pending," not fake a number. Shipped exactly that: empty track, "pending data source" badge. |
| Overview tab sub-agent tabs (`main` / `test-runner` / `log-search skill`) | Needs Claude Code's beta trace-span join, explicitly not a v1 requirement per the plan; the plan says the tab UI should "gracefully degrade to just main" until that lands — which is exactly what shipped. |
| Overview tab: Active time / Lines changed metric tiles | No backing data field exists for either; omitted rather than shown as fake zeros — a defensible call, though still a visible difference from the mockup's 6-tile layout. |
| Breakdown tab, 3 of 4 subpanels | The plan names all 4 subpanels as build items, but "Tool reliability" (the one implemented) is the only one with a real, already-available data source; the other 3 (spend-by-subagent, MCP connections, reliability signals) read "Not tracked yet," honestly labeled rather than faked. |

## One thing the shipped version does *better* than the mockup

**Context Explorer block list**: the mockup's blocks are static — you see
labels, token counts, and percentages, but can't click through to the
actual content. The shipped version added click-to-expand, showing the
real captured system prompt / user message / assistant response text
inline, with proper HTML-escaping. This isn't in the mockup at all —
matches and exceeds it.

## Source

Everything above was checked directly against:
- The mockup artifact: `https://claude.ai/code/artifact/0a091cd1-0140-45d6-92ae-0650a9668b80`
- The shipped rendering code: `mcp_server/server.py`'s `renderKpiStrip`,
  `renderQuotaStrip`, `renderSessionRow`, `renderSessionListPanel`,
  `renderOverviewTab`, `renderBreakdownTab`, `renderContextTab` functions.
- The build plan: `docs/internal/OTLP_INTEGRATION_PLAN.md`.

No item above is asserted from memory or assumption — each was read
directly from one of the three sources listed.
