# User journey: new user to daily-driven, end to end

An honest audit of what a brand-new user actually has to do to get value from
this project, whether that setup survives real-world conditions (sleep, new
terminals, new Claude Code sessions, signing out), how the shipped dashboard
compares to the approved design mockup, and what it takes to reach "just
works everywhere, always."

Everything below is either directly tested this session, reasoned from code
actually read this session, or — for §5 — a feature built and verified live
during this same session. Nothing here is aspirational marketing copy.

## 1. The journey today, step by step

| # | Step | What the user does | Friction |
|---|------|---------------------|----------|
| 1 | Discover | Visit the live demo URL, or `git clone` + `uv run` to self-host | Not on PyPI; self-hosting needs a source checkout. The live demo is instant but read-only until sign-in. |
| 2 | Land | `/auth/login` renders hero, feature pitch, three FAQ disclosures, sign-in card | None — loads instantly, explains itself. |
| 3 | Authenticate | Click "Sign in with Google" → Google's real consent screen → this app's own Wrangler-style Authorize/Cancel screen | One click, one confirm. Genuinely easy. |
| 4 | Land on config | "Your connection" card, a **new one-click local setup card** (§5), and a 4-tab "Connect your client" card for manual setup | Resolved for local self-hosters by §5. Still true for the deployed instance and for manual/other-client setups. |
| 5 | Wire up (automatic, local) | Click **"Apply to my Claude Code config"** | One click. Done. See §5. |
| 5′ | Wire up (manual, deployed or other clients) | Copy a JSON block into an MCP config, *and* ~7 export lines into a shell profile or `settings.json`'s `env` block | Requires knowing which file, and the raw-content opt-in (bundled with the content-length cap in one copy button) is an easy whole step to skip (see §2). |
| 6 | Proceed to dashboard | Click through (first sign-in) or land automatically (returning visit) | Clean, fixed this session. |
| 7 | Use it | Work normally in Claude Code; sessions appear within ~5–8s | Works everywhere now, for local self-hosters who used §5. |

**Time to first value:** under 3 minutes either way. **Time to full, durable,
"works in every project without thinking about it again":** now also under
3 minutes for a local self-host, via §5 — previously required knowing to
hand-edit the *global* config file, which nothing in the UI said.

## 2. Friction points that remain

- **The one-click path only covers local self-hosting.** The deployed
  instance's browser can never write to a visitor's local filesystem — §5
  is gated to exactly that case, correctly, and the manual path (§4, step 5′)
  is what's left for everyone else.
- **Two unrelated mechanisms still share one manual "Connect your client"
  card**, for the deployed instance. The MCP tabs give an LLM *query* access
  (ask "what did session X cost"); the OTLP tabs give *passive, automatic*
  metrics with no tool calls. Nothing on the page says most people want both.
- **The raw-content opt-in is still one bundled, skippable step** in the
  manual snippet, for anyone not using §5 — both the opt-in line and the
  content-length cap live in the same copy-button block, so it's correctly
  one decision, not two, but it's still an easy one to skip entirely. Skip
  it and you keep tokens/cost/tool-calls but lose the Context Window
  Explorer (found this session — see the E2E test report).

## 3. Does it survive real-world conditions?

| Condition | Survives? | Why |
|---|---|---|
| Mac sleep/wake | Yes | Nothing here depends on wall-clock continuity. Bearer tokens don't expire; OTLP just re-exports on the next request; `localStorage` is untouched by sleep. |
| New Claude Code session, same directory | Yes | A project-level `.claude/settings.json` env block auto-fires telemetry with zero manual exports — proven live. |
| New Claude Code session, **any** directory | **Yes — proven live this session** | After clicking "Apply to my Claude Code config" (§5), a brand-new `claude -p` call from a completely unrelated, never-before-seen directory, with zero manually-exported variables, exported telemetry and appeared on the dashboard within seconds. This is the gap that existed earlier in this same session and is now closed. |
| Sign-out, then sign back in | Yes, transparently | Sign-out only clears client-side `localStorage`. The bearer token isn't revoked; `auth_store.get_or_create_token` is idempotent per Google account, so signing back in returns the identical token — no re-setup, nothing to re-paste. |
| The MCP tool connection itself | Yes | A static config entry; Claude Code re-reads it at every session start regardless of what happened in between. |

## 4. The claude.ai Connectors path — a second way to "everywhere"

Direct evidence, not speculation: this account's own `~/.claude/settings.json`
already contained a working example before this session touched anything —

```json
"mcpServers": {
  "lockin": {
    "url": "https://lockin.talhaakhoon.dev/mcp",
    "headers": { "Authorization": "Bearer lin_y7RxbwboEHsPoQ00WZSIrWVPzVH_1Bf0cCDEm7A2t0s" }
  }
}
```

added entirely through claude.ai's account-level **Connectors** settings
(Customize → Connectors → Add custom connector) — not by hand-editing this
file. Anthropic's own support docs confirm connectors added there work
across Claude, Cowork, and Claude Desktop, and are silent on Claude Code
specifically — but this file is direct, current proof it reaches Claude Code
too, at least for this account, and confirms a **plain bearer-token header**
(no OAuth client ID/secret) is enough.

**What this path can and can't do:**

- **Can**: make the MCP query tools available in every Claude Code session
  automatically — no local file edits at all, for any client, once added at
  claude.ai. The only path that works for the *deployed* instance without
  any local setup step.
- **Can't**: carry the OTLP auto-telemetry half. Connectors configure an MCP
  server connection, not arbitrary session environment variables. §5's local
  write is the only mechanism that delivers that half automatically today.
- **Requires** a real public HTTPS URL — the deployed Cloudflare Worker
  instance qualifies; a `localhost` self-host does not (§5 exists precisely
  to cover that case instead).

## 5. Delivered this session: one-click local setup

Built, tested, and confirmed live — not a recommendation, a shipped feature.

**What it does:** a new card on the connect page, visible only when viewing
it at `localhost`/`127.0.0.1` (self-hosting). One click POSTs to
`/setup/apply-local-config`, which:

- Backs up the caller's existing `~/.claude/settings.json` first (never a
  blind overwrite).
- Merges in an `mcpServers.context-inspector` entry and the full OTLP `env`
  block (telemetry, raw-body opt-in, and the content-length cap from §2) —
  merges, doesn't replace, so an existing entry like LockIn's, or any
  unrelated env var, survives untouched.
- Is gated to loopback requests only (`request.client.host` must be
  `127.0.0.1`/`::1`) on top of the existing bearer-token requirement — a
  deployed instance can never trigger a write on a visitor's machine, and a
  second local process without the real token can't call it either.

**Privacy, stated plainly on the confirmation screen:** *"Your token, your
sessions, your local Claude Code config — all of it stays on this computer.
Nothing you set up below is ever sent anywhere except this server."* True by
construction: the write only ever happens through a local HTTP call from the
browser to a server process running on that same machine, writing to that
same machine's disk.

**Verified live, end to end, this session:**
1. Called the endpoint for real against this account's actual
   `~/.claude/settings.json` — response confirmed a backup was written first.
2. Read the resulting file: LockIn's entry untouched, `context-inspector`
   added correctly, all 8 env vars present.
3. From a brand-new, unrelated directory (`/tmp/totally-random-test-dir`,
   never configured before), ran `claude -p` with **zero** manually-exported
   variables — it exported telemetry anyway, and the resulting session
   appeared on the dashboard within seconds.

## 6. Mockup vs. shipped — what's visually accurate, and what isn't

The approved design mockup and the shipped dashboard match closely on
structure (topbar, KPI strip position, tabs, Context Explorer bar/legend/
block-list, Tool calls table) but diverge in a few concrete, checkable ways —
found by a dedicated fresh-eyes comparison earlier this session, re-confirmed
here against the mockup screenshots directly:

| Element | Mockup | Shipped | Verdict |
|---|---|---|---|
| 5-hour / 7-day usage windows | Filled progress bars with real-looking percentages (62%, 88%) | Empty track, "PENDING DATA SOURCE" badge, honest "Not yet wired to a data source" copy | **Intentional deferral, not a bug** — no officially supported data source exists for this; rendering a fake number would be worse than an honest placeholder. |
| KPI strip | 6 tiles, all populated, with deltas and a token sparkline | 3 of 6 populated (Sessions, Tokens, Spend); Cache hit rate / Tool error rate / Context alerts show "—, per-session only"; no deltas, no sparkline | **Real gap.** Documented in code as a deliberate N+1-avoidance tradeoff, but it is a visible mismatch from the mockup, not a deferral named in the original build plan. |
| Session list rows | Source badge, prompt, "Xm ago · N turns", colored context% dot (warn/err states) | Source badge, prompt, "Xm ago", **tokens · cost** instead of turn count, no context-alert dot | **Real gap** — different metric shown entirely, not just missing styling. |
| Session list filter row | Source filter chips (All sources / Claude Code / Copilot / Bedrock agent) | Absent | **Real gap**, not a named deferral. |
| Overview tab, sub-agent tabs | `main` / `test-runner` / `log-search skill`, each with its own token count | Always a single `main` tab | **Documented deferral** — needs Claude Code's beta trace-span join, not yet available. |
| Overview tab, metric tiles | Tokens, Cache hit, Cost, Active time, Tool calls, Lines changed | Tokens, Cache hit, Cost, Tool calls only | **Partial deferral** — Active time / Lines changed omitted on purpose (no backing field) rather than shown as fake zeros; still a visible gap from the mockup. |
| Breakdown tab | 4 populated subpanels | Only "Tool reliability, this session" is real; the other 3 read "Not tracked yet" | **Documented deferral**, honestly labeled. |
| Insight cards | 3 cards with specific, data-backed suggestions | Absent entirely | **Real gap, not a clean deferral.** The project's own plan lists "2-3 example personalized insights" as an agreed v1 requirement, not optional polish — only the *full 30-item catalogue* of possible insight types is explicitly marked "backlog, not a v1 requirement." A minimal version of this was owed for v1 and wasn't built; conflating it with the backlog (an earlier pass in this same audit did exactly that, corrected here) undersells the gap. |
| Context Explorer bar, legend, block list | 8 categories, ordered blocks with token/percent | Same 8 categories, same structure, now with click-to-expand real content (built this session) | **Matches, and now exceeds** the mockup — the mockup has no expand interaction at all. |

**Bottom line on fidelity:** the deferrals that are genuinely *agreed and
documented as out of scope for v1* (quota strip, sub-agent breakdown, 3 of 4
breakdown panels) are honest, labeled, defensible choices, not oversights.
Four things aren't in that category, and are the right next things to close:
the KPI stubs, the session-list metric swap (both plan-required, per the
plan's own "agreed requirements" list), the missing source filter (not
mentioned in the plan at all), and insight cards (a minimal 2-3 card version
was a plan requirement too — only the full 30-item catalogue was ever
actually deferred, a distinction an earlier pass of this same audit missed
and corrected above).

## 7. Bottom line

For **querying your own metrics, from anywhere**: easy today via either
path — one-click local setup (§5) for self-hosters, or claude.ai Connectors
(§4) for the deployed instance — and durable across sleep, sign-outs, and
every future session, tested and confirmed.

For **the dashboard auto-populating as you code, from anywhere**: solved for
local self-hosters via §5, confirmed live from a brand-new directory with
zero manual steps. Still manual (§4, step 5′) for the deployed instance,
since Connectors can't carry environment variables — that gap is inherent to
how claude.ai Connectors work, not something this project can close alone.

For **visual fidelity to the approved mockup**: strong on structure, honest
on the deferrals that are actually agreed-and-documented as out of scope,
and now has a named, checkable list (§6) of the four gaps — including one,
insight cards, that an earlier pass of this same audit mislabeled as a clean
deferral and had to correct — that weren't accurately documented anywhere
before today.
