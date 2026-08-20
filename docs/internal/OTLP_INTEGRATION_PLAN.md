# OTLP Telemetry Integration — Build Plan

Status: planning complete, not started. Written for a fresh context window to build
against without re-deriving the research. Everything in this doc is either confirmed
against live docs (cited) or explicitly marked unverified.

## Design reference

An approved dashboard mockup exists as a Claude Artifact:
**https://claude.ai/code/artifact/0a091cd1-0140-45d6-92ae-0650a9668b80** — two screens
(Dashboard, Project settings), built against real CSS tokens from this project's own
`_PAGE_STYLE` (not a fantasy redesign). If this link no longer resolves by the time
this plan is picked up, ask the user for it — it was shared and approved during
planning, not authored fresh here. **The specific UI structures below are what that
mockup actually shows and what was agreed on** — build to it, don't redesign the
dashboard from scratch based on the prose elsewhere in this doc alone.

## Why

`mcp-context-inspector` currently only gets data via an explicit `record_session` call.
This looked automatic for our own Bedrock-based agent, but wasn't — it's fine there
only because the agent's own runtime code (`agent/runtime.py`) actively builds the
`trace`/`turns`/`context_blocks` objects itself, turn by turn, as it runs its own
Converse-API loop, then hands the finished object to `record_session` in one call at
the end. The "automatic" feeling comes entirely from that being our own code with
direct access to its own execution — not from any passive observation. It cannot do
this for a generic chat client (Claude Code, Copilot, etc.) connected via MCP, because
MCP servers only ever see requests explicitly sent to them — confirmed against the
current MCP spec (2026-07-28): Logging, Sampling, and Roots were just deprecated with
the explicit guidance "use OpenTelemetry instead of MCP" for this kind of
observability. There is no MCP-level fix for this.

The real fix: ingest the OpenTelemetry export these clients already emit natively,
outside the MCP connection entirely. Confirmed two real targets exist (see Research
Findings below): **Claude Code** and **GitHub Copilot**. Cursor does not — its OTel
export explicitly excludes prompt/response content and per-session tool attribution, so
it can only ever power a coarse token/cost dashboard, not the context-window explorer.
Not worth a build track.

## Competitive positioning (why this is worth building)

Researched live (Aug 2026): a **live, OTel-native, block-by-block ordered
context-window-composition explorer** does not exist yet for either Claude Code or
Copilot. What exists instead:
- A crowded field of **aggregate token/cost dashboards** (ccusage, half a dozen
  `claude-code-otel`→Grafana/Prometheus variants, SigNoz, AWS CloudWatch's Claude Code
  integration) — none show context composition, just spend/usage numbers. Not
  differentiated; don't compete here.
- **Context Lens** (github.com/larsderidder/context-lens) is the one real close
  competitor — genuine per-session composition breakdown (system/tools/history/tool
  results), but via **local HTTP proxy interception** (`ANTHROPIC_BASE_URL` override),
  rendered as a treemap + turn-diffs, after-the-fact, not a live ordered timeline. Our
  differentiation: OTel-native (no proxy, works with existing enterprise pipelines,
  lower friction) + strictly time-ordered live block list instead of a treemap.
- Anthropic's own "Explore the context window" demo page is static/hardcoded, not a
  real tool — inspiration only, not competition.

## Research findings (verified, cited — do not re-derive)

### Claude Code (docs.claude.com/en/monitoring-usage, fetched live)

- `CLAUDE_CODE_ENABLE_TELEMETRY=1` enables export. **No default transport protocol** —
  must explicitly set `OTEL_EXPORTER_OTLP_PROTOCOL` (`grpc` | `http/json` |
  `http/protobuf`). Use `http/json` server-side — no protobuf dependency needed.
- `OTEL_LOG_RAW_API_BODIES=1` (inline, 60KB truncation, `CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH`
  to tune) or `OTEL_LOG_RAW_API_BODIES=file:<dir>` (untruncated, writes to
  `<dir>/<uuid>.request.json` / `<dir>/<request_id>.response.json`, event carries a
  `body_ref` path). **Use file mode** — inline truncation will clip real sessions with
  substantial tool specs/history.
- Bodies are the **literal Anthropic Messages API JSON** — `system`, `messages[]`
  (with `text`/`tool_use`/`tool_result` content blocks), `tools[]`. Maps ~1:1 onto our
  existing `agent/runtime.py`-style categorization (system/tools/user/tool_call/
  tool_result/answer) by walking blocks in array order.
- **Caveat**: extended-thinking content is **unconditionally redacted**, even with raw
  bodies on. Reasoning blocks will have a size/token estimate but no text — render as
  "reasoning (redacted)", don't treat as a bug.
- **Correlation hierarchy**: `session.id` (every metric/event, one Claude Code
  conversation) → `prompt.id` (UUID v4, one user turn) → `request_id`/`client_request_id`
  (pairs `api_request_body`/`api_response_body`) / `tool_use_id` (tool calls). Group
  ingested data this way.
- Metrics: `claude_code.token.usage` (attrs: `type` = input/output/cacheRead/
  cacheCreation, `model`, `session.id`, ...), `claude_code.cost.usage`. Prefer the
  response body's `usage` block for exact counts; cross-check against these metrics.
- Events (`event.name`): `api_request_body`, `api_response_body`, `tool_result`,
  `user_prompt`, `assistant_response`, `mcp_server_connection`, etc. `event.sequence`
  is a monotonic per-session counter — use for ordering if timestamps collide.
- Batching: metrics flush every `OTEL_METRIC_EXPORT_INTERVAL` (default 60s), logs every
  `OTEL_LOGS_EXPORT_INTERVAL` (default 5s) — near-live, not per-call-instant. Both
  tunable down for a snappier dashboard.
- Privacy layering (for our onboarding copy, not just internal knowledge):
  `OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_ASSISTANT_RESPONSES` / `OTEL_LOG_TOOL_DETAILS` /
  `OTEL_LOG_TOOL_CONTENT` (each off by default, narrower disclosure) vs.
  `OTEL_LOG_RAW_API_BODIES` (superset — implies consent to all of the above, "bodies
  include the entire conversation history"). Be explicit about this tiering in the UI.

**Unverified, needs one real local capture before finalizing the parser**: exact
byte-level JSON shape in practice (retry behavior — doc says "one event per attempt"
but no worked example, exact truncation-marker format, how OTLP JSON encodes log
attributes). Do this first: run `claude` locally with
`CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp OTEL_EXPORTER_OTLP_PROTOCOL=http/json OTEL_LOG_RAW_API_BODIES=file:/tmp/cc-bodies`
pointed at a throwaway local HTTP listener (or a minimal OTel Collector with a
`debug`/`file` exporter), capture one real payload, and diff it against the assumptions
above before writing the production parser.

#### Subagent context-window granularity — confirmed possible, but only via beta traces

Verified (2026-08) against `code.claude.com/docs/en/monitoring-usage`, the Agent SDK
observability guide, and the `anthropics/claude-code` CHANGELOG directly:

- A subagent's own model call **does** produce its own genuine
  `api_request_body`/`api_response_body` event — its own system/messages/tools, not a
  slice of the parent's. `query_source` (`main`/`subagent`/`auxiliary`) confirms
  subagents are logged as distinct request-issuing subsystems.
- **But `agent.name`/`skill.name` are NOT present on the raw body events themselves**
  — checked attribute-by-attribute. They only appear on the metrics and on the
  separate `api_request`/`api_error` events. `query_source` on the body event only
  gives a coarse bucket (its own documented example value is `"compact"`, not a named
  subagent) — not enough alone to attribute a captured payload to *which* subagent.
- **The actual correlation mechanism is real but beta**: since Claude Code v2.1.139,
  subagent API requests carry `agent_id`/`parent_agent_id` on `claude_code.llm_request`
  and `claude_code.tool` **trace spans** (not on the log events) — gated behind
  `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`. Log events carry a matching `trace_id`/
  `span_id` when trace propagation is active (reliable from v2.1.212+), so the join
  path is: raw-body log event → shared `trace_id`/`span_id` → trace span carrying
  `agent_id`/`parent_agent_id` → attribute the body to a specific subagent.
- `session.id` does not fork for subagents (same session, no documented exception) —
  so subagent detection/attribution has to come from this span join, not from a
  different session identifier.

**Verdict: buildable with caveats, not a v1 requirement.** Requires opting into beta
tracing, a Claude Code version floor (v2.1.212+ for reliable event/span joining), and
span/attribute names that are explicitly subject to change while beta. Build the base
Claude Code mapper first without subagent attribution (bucket everything under
`query_source` coarsely: main vs. subagent vs. auxiliary, no per-subagent breakdown);
add the trace-span join as a follow-up once the beta surface stabilizes. Don't block
v1 on this.

#### 5-hour / 7-day usage-window percentage — not achievable via any supported path

Verified (2026-08): **no officially documented/supported mechanism exists.**

- Not in the OTel metric list (`claude_code.token.usage`, `.cost.usage`,
  `.session.count`, `.lines_of_code.count`, `.pull_request.count`, `.commit.count`,
  `.code_edit_tool.decision`, `.active_time.total` — confirmed exhaustive, no
  quota/rate-limit metric among them).
- The `anthropic-ratelimit-*` HTTP headers are real, but describe a **completely
  different system** — per-minute org/API-key token-bucket limits (RPM/ITPM/OTPM), not
  the Pro/Max subscription's rolling 5h/weekly usage window. And raw-body OTel capture
  only exports the JSON body plus a few derived fields — it does **not** capture HTTP
  headers at all, so even if it were the right data, this path wouldn't carry it.
- The Anthropic Admin `Rate Limits`/`Usage and Cost` APIs are real and documented but
  explicitly **"unavailable for individual accounts"** — org-only, Admin-key-gated,
  and about org-configured limits, not personal subscription quota.

**One real but explicitly unofficial path exists, use-at-own-risk**: Claude Code's own
`/status` display calls an **undocumented internal endpoint**,
`GET https://api.anthropic.com/api/oauth/usage`, with the user's own OAuth token
(readable locally from `~/.claude/.credentials.json`) plus an
`anthropic-beta: oauth-2025-04-20` header and a `claude-code/<version>`-style
`User-Agent` (omitting it routes into an aggressive rate-limit bucket). It returns
exactly `five_hour: {utilization, resets_at}` and `seven_day: {utilization,
resets_at}` (plus per-model breakdowns and an `extra_usage` field) — confirmed
working today, and used in production by at least one third-party tool
(`Maciek-roboblog/Claude-Code-Usage-Monitor`). **But**: Anthropic closed a bug report
against this exact endpoint as "not planned" with no commitment to stability, no ToS
blessing, and no SLA — it can change, 429, or be blocked without notice at any time.

**Verdict: not currently possible as a supported feature.** If we want the 5h/7d
widgets at all, that's a deliberate risk tradeoff to make explicitly, not something to
quietly build in — flag it clearly in the UI ("unofficial, may break") if built, or
drop the widgets from v1 and revisit only if Anthropic documents this later. Don't
present the mocked-up quota widgets from the dashboard mockup as confirmed-buildable
without this caveat attached.

### GitHub Copilot (code.visualstudio.com/docs/agents/*, docs.github.com/copilot/*, fetched live)

Genuinely at parity with or ahead of Claude Code's telemetry — build this second.

- Enable: `github.copilot.chat.otel.enabled` setting or `COPILOT_OTEL_ENABLED=true`,
  or just setting `OTEL_EXPORTER_OTLP_ENDPOINT`. Standard OTel env vars all apply.
- **Structured content capture** (opt-in): `github.copilot.chat.otel.captureContent` /
  `COPILOT_OTEL_CAPTURE_CONTENT=true` exposes `gen_ai.input.messages`,
  `gen_ai.output.messages`, `gen_ai.tool.definitions`, `gen_ai.tool.call.arguments`/
  `.result` — semantic content, not raw HTTP bodies. Arguably *easier* to parse than
  Claude Code's raw Messages API JSON since it's pre-structured.
- **Traces**: hierarchical span tree per turn — `invoke_agent` → `chat` (per LLM call)
  → `execute_tool` (per tool call, **including MCP tool calls**, with server/tool
  name/args) → `execute_hook`. Tool-call attribution, including MCP-sourced calls, is
  native — no separate gap to work around.
- Metrics: `gen_ai.client.token.usage`, `gen_ai.client.operation.duration`,
  tool-call counts/durations, TTFT.
- Hooks exist too (`PreToolUse`/`Stop`/etc., same shape as Claude Code's) — not needed
  given the OTel path is sufficient, but available as a fallback.
- Enterprise-managed OTel (shipped 2026-07-08): org admins can centrally mandate the
  endpoint/policy via `managed-settings.json` — relevant for a "fleet" pitch later, not
  needed for individual onboarding.
- VS Code's native AI surface **is** Copilot Chat — no separate VS Code integration
  needed; building this covers VS Code automatically.

**Mapper differs structurally from Claude Code's**: parse `gen_ai.*` span/event
attributes directly (already-structured message/tool objects) rather than parsing a
raw Messages-API-shaped JSON blob. Build as a second, separate mapper module sharing
the same downstream session/turn/context_block schema.

### Cursor — explicitly out of scope for the composition dashboard (cursor.com/docs/enterprise/opentelemetry-export, cursor.com/help/customization/extensions, cursor.com/blog/marketplace, fetched live)

OTel export exists but is Enterprise-plan-gated, admin-configured only (no per-dev
self-serve), and its own docs explicitly state: **no prompt/response content, no
traces, no historical backfill**, and metrics carry no correlation ID linking a
token-usage datapoint to a specific conversation (only the logs stream has
`cursor.conversation.id`, and metrics are at-most-once/lossy). Token counts and
tool-call *counts* only — same tier as ccusage, not differentiated. **Do not build a
Cursor track now.** Could revisit later as a bolt-on coarse usage widget only if
someone asks, not as part of this plan.

## Hosted-client integration (Claude.ai, ChatGPT, Cursor) — MCP-native, not OTel

Claude.ai and ChatGPT have no OTel export at all (confirmed — hosted infrastructure,
nothing local to point at our endpoint), and Cursor's export explicitly excludes
content (see above). None of the OTel receiver work above reaches these clients. But
MCP itself gives us two real, honest, MCP-native mechanisms — deliberately not trying
to fake "automatic" for products that structurally can't do automatic.

### 1. MCP Activity Log — real today, but scoped narrower than it sounds

Every Streamable HTTP MCP connection carries an `Mcp-Session-Id`, and every call to
*our own* tools is already fully visible to us — timestamp, tool name, exact args,
latency, result. This is **not** visibility into the rest of a user's conversation or
their token usage — it's a log of how a client used *our* tools specifically. This is
the distinction that matters: a friend's Claude Code connecting and calling only our
read tools (as happened before any of this OTLP work existed) would show up here as
"connected, called `get_recent_sessions` → 0 results, disconnected" — accurate, but
not what anyone actually wants to see. **To be explicit: this feature would NOT have
resolved that original incident.** It only ever shows calls made to our own tools —
never a client's own token usage, tool calls to other systems, or conversation
content. Build this as a clearly separate "Server usage" dashboard section
(operator/owner view — who's using the MCP server and how), never folded into
session/context data, so it's never mistaken for the real thing.

### 2. `transfer_context` MCP Prompt + `import_chat_session` tool — the real answer for hosted clients

MCP Prompts (`prompts/list`/`prompts/get`) are a real, currently-supported part of the
spec (unlike Logging/Sampling/Roots, which were deprecated in the 2026-07-28
revision) — a client that supports them (Claude.ai, Claude Desktop, Cursor) surfaces a
server-defined prompt as something a user can trigger (slash command / picker), which
inserts pre-written instruction text into the conversation as if the user had typed it.

**Why this actually works, not just "the model guesses a percentage":** the model
generating a response has the literal text of the prior conversation in its own
context right now — it's not recalling from a hazy memory, it's summarizing content
that is, at the moment of the tool call, part of its own input. So a prompt that
explicitly instructs a real reconstruction gets real content back, not a vibe.

**Design:**
- A prompt (e.g. `transfer_context`) with instruction text along the lines of: *"Walk
  through this conversation turn by turn. For each turn, summarize the user's message
  and your response. List every tool call you made, with the tool name, a summary of
  its arguments, a summary of what it returned, and a rough size estimate. Flag
  anything you suspect this platform already dropped or compacted before this point,
  rather than guessing at content you don't actually have."* Invoked by the user
  mid-conversation, deliberately, at whatever point they choose (e.g. right before
  they expect to run out of room) — not automatic, not continuous.
- A new tool, `import_chat_session(source_platform, turns[], tool_calls[],
  context_note)`, that the model calls with the reconstruction. Server maps this onto
  the same `context_blocks` shape everything else uses (category `user`/`answer`/
  `tool_call`/`tool_result`, size estimates via the same chars-per-token heuristic
  `agent/runtime.py` already uses for its own estimates) — same downstream schema,
  same dashboard rendering, no special-cased UI.
- **New, distinct `source` value: `self_reported_import`** — never merged into
  `claude_code`/`copilot`/`bedrock_agent`. Render with a clear "reconstructed by the
  model from its own recall, not measured" badge — same honesty pattern already used
  for redacted-thinking blocks. This data has fundamentally different provenance and
  confidence than instrumented OTel data and must never look identical to it.

**Two honest limits — communicate these, don't paper over them:**
1. If the platform already silently compacted/dropped earlier turns before the user
   invokes this, that content is genuinely gone — the model can only report what's
   still actually present in its live context at invocation time.
2. **This does not solve the 5h/7d usage-window problem.** That's the underlying
   product's own CLI/UI chrome (`/status` output), never part of what the model itself
   sees in the conversation — the model can reconstruct what it experienced, not
   surface a number it was never shown. The 5h/7d question stays exactly as scoped in
   the Claude Code research findings above (unsupported, one risky unofficial
   endpoint) — unaffected by this mechanism.

This is genuinely the strongest available story for Claude.ai/ChatGPT/Cursor: one
clean, user-triggered action, real reconstructed content rather than a self-reported
percentage, works identically across any MCP-prompts-supporting client since it's a
spec-level primitive, not vendor-specific plumbing.

## Architecture

Reuse everything already built — auth, storage backends, dashboard rendering. Add:

1. **Generic OTLP ingestion route(s)** in `mcp_server/server.py`:
   `POST /otlp/v1/metrics`, `POST /otlp/v1/logs` (and `/traces` if useful for Copilot's
   spans). Add `/otlp` to `MultiTokenAuthMiddleware`'s `protected_prefixes` — same
   per-user bearer token already used for `/mcp` and `/api/`, passed via
   `OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"`. One identity, one
   token, works everywhere. No new auth mechanism.

2. **Incremental store writes** — the real schema change. Today `record_session()`
   assumes one atomic insert with the full `trace`/`turns`/`context_blocks` known
   upfront (fine for our Bedrock agent, which reports once at the end of its loop).
   OTLP telemetry arrives continuously, turn by turn, for a session that's still
   ongoing. Add to `metrics/store.py` (**both** `store_sqlite.py` and
   `store_dynamodb.py` — keep the "same signature either way" contract):
   - `start_or_get_session(session_id, owner, source, model=None) -> session_id`
   - `append_turn(session_id, turn_data)`
   - `append_tool_call(session_id, tool_call)`
   - `append_context_block(session_id, block)`
   - `close_session(session_id, final_totals)` (optional — mark complete once the
     client process exits or goes idle past some timeout; dashboard should already
     tolerate an "open" session showing partial data given it already polls)
   - Migration: add a `source` column to `sessions` (`bedrock_agent` | `claude_code` |
     `copilot`), mirroring the existing `owner`/cache-columns migration pattern in
     `store_sqlite.py` (`_migrate_sessions_table`-style idempotent `ALTER TABLE`).
     Existing rows get `source='bedrock_agent'` (or NULL treated as that) for
     backward compatibility — don't break `get_recent_sessions` for existing data.

3. **Per-vendor mapper modules** — new package, e.g. `mcp_server/otlp/`:
   - `claude_code.py`: parses OTLP payloads keyed by Claude Code's schema (session.id/
     prompt.id/request_id hierarchy, raw Messages API body walking) into the
     append_* calls above.
   - `copilot.py`: parses `gen_ai.*` span/event attributes into the same append_*
     calls.
   - Shared dispatch: inspect resource attributes (e.g. `service.name` or similar — a
     Copilot payload and Claude Code payload should be distinguishable by a resource
     attribute; confirm exact value during the empirical capture step) to route to
     the right mapper.

4. **Dashboard**: already source-agnostic (polls `/api/sessions` etc.) as a data
   pipeline, but the mockup (see Design reference above) specifies real UI structure
   beyond what currently exists that must actually get built, not just implied —
   these are agreed requirements, not optional polish:
   - **Time-range filter**: Today / Last 7 days / Last 30 days / All time — a chip
     row scoping both the KPI strip and the session list, not just a cosmetic label.
   - **KPI strip**: sessions count, tokens, spend, cache-hit-rate, tool-error-rate,
     and context-alert count, each for the selected time range — the aggregate view
     sitting above the session list.
   - **Session list**: per-source badge (Claude Code / Copilot / Bedrock agent) per
     row, matching "3. Dashboard" above, PLUS an inline context-pressure indicator
     (e.g. a colored dot + %) directly in the row — not hidden behind a click, since
     catching pressure before it bites is the whole point of the Explorer.
   - **Session detail: four tabs**, not one flat view — **Overview** (metric tiles +
     which-agent-window selector), **Context Explorer** (the existing bar/legend/
     block-list), **Tool calls** (a full per-call trace table: #, tool, status,
     latency, args, timestamp — new, doesn't exist today), **Breakdown** (attribution
     panels: spend-by-subagent/skill, tool reliability, MCP server connections,
     reliability signals — also new).
   - **Sub-agent tabs** within Overview/Context Explorer, to switch which agent's
     context window is being viewed (main / a named subagent / a named skill) — this
     UI element exists in the approved mockup, but per the "Subagent context-window
     granularity" section above, the *data* behind it (per-subagent raw-body
     attribution) requires the beta trace-span join and is not a v1 requirement.
     Build the tab UI now if convenient, but it should gracefully degrade to just
     "main" (no tab switcher shown) until the beta join lands — don't block the tab
     UI on the beta data, but don't fake subagent data either.
   - **Quota strip (5h/7d)**: per the "5-hour/7-day usage-window" verdict above, this
     is an explicit open decision, not resolved by this plan. If built, it must be
     visually marked "unofficial, may break" per that section — don't ship it looking
     like a confirmed, stable metric.
   - **Insight cards**: 2-3 example personalized insights (see "Personalized
     insights" section) rendered above or alongside the session list.
   - **Project settings screen** (separate from the main dashboard): per-project
     alert thresholds (context %, cost, tool-error-rate), a default content-masking
     toggle, a raw-body retention period selector, and manual session tags — per the
     "Per-conversation / per-prompt configuration" section above.
   No structural rewrite of the *data layer* is needed — the tabs/filter/panels above
   are new DOM/rendering work on top of the existing `/api/*` responses, not a new
   backend. Build against the mockup's actual markup/CSS as a starting point rather
   than reinventing the layout.

5. **Onboarding UX** — on the `connectPage()` post-Authorize screen (already has
   "Claude Code" / "API / curl" tabs after the recent scope-narrowing cleanup, see
   Prerequisites below): add a **"Claude Code (live telemetry)"** tab and a
   **"Copilot (live telemetry)"** tab, each with a copy-pasteable shell snippet using
   that user's own token, e.g.:
   ```
   export CLAUDE_CODE_ENABLE_TELEMETRY=1
   export OTEL_LOGS_EXPORTER=otlp
   export OTEL_METRICS_EXPORTER=otlp
   export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
   export OTEL_EXPORTER_OTLP_ENDPOINT=https://<host>/otlp
   export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"
   export OTEL_LOG_RAW_API_BODIES=1
   ```
   Ship the "raw bodies" line as a **separate, clearly-labeled, opt-in** toggle/button
   in the UI (not bundled silently into the base snippet) — per Claude Code's own
   privacy-tiering docs, this is a materially bigger disclosure than token counts
   alone ("bodies include the entire conversation history"). Reuse the existing
   `copyText()`/tab pattern already in `connectPage()`.

## Maximizing telemetry — beyond the base token/cost/context pipeline

The schemas already researched carry more than the original plan captured. All of the
following are confirmed real attributes/events (not speculative) — worth capturing now
rather than bolting on later, since the mapper is being written from scratch anyway.

**New metric categories to store and surface, per session:**
- **Cache efficiency** — `cacheRead`/`cacheCreation` vs. plain `input` token counts are
  already separate dimensions on `claude_code.token.usage`. Surface as a cache-hit-rate
  stat, not just buried in the raw token tiles — this is a real cost-saving signal
  users will care about.
- **Tool reliability** — per-tool success/error rate and p50/p95 latency, from
  `tool_result`'s `success`/`duration_ms`/`error_type` (Claude Code) and
  `execute_tool` span duration (Copilot). Already storing individual tool calls; this
  is just an aggregation view, not new capture.
- **Code impact** — `claude_code.lines_of_code.count`, `claude_code.commit.count`,
  `claude_code.pull_request.count` are real, confirmed metrics with no equivalent in
  the current schema. Store and show as "what did this session actually produce,"
  distinct from token spend — genuinely differentiating, no competitor surfaces this.
- **Edit acceptance/survival** (Copilot-specific) — `gen_ai.*` edit-acceptance metrics
  plus "survival" (did a suggested edit still exist N minutes/commits later, i.e. not
  immediately reverted). This is a real quality signal, not just volume — worth its
  own tile, not averaged away into a generic "tool calls" count.
- **Subagent/skill/plugin attribution (metrics-level, v1)** — `query_source`
  (main/subagent/auxiliary), `agent.name`, `skill.name`, `plugin.name`,
  `marketplace.name` are all real dimensions on Claude Code's token/cost **metrics**
  (confirmed present there, not on the raw body events — see the codebase note above).
  Store them and let a session's detail view break down "which subagent/skill actually
  spent these tokens" as an aggregate bar chart — meaningful for anyone using Claude
  Code's subagent/skill features, buildable now, no beta flags needed. **This is not
  the same thing as per-subagent context-window composition** (a separate Context
  Window Explorer per subagent, block-by-block) — that requires the beta trace-span
  join described in "Subagent context-window granularity" above and is explicitly
  deferred past v1. Don't conflate the two: this bullet is spend/token totals by
  subagent name; the deferred one is each subagent's own detailed context breakdown.
- **MCP server health, meta** — `claude_code.mcp_server_connection` (status,
  transport_type, duration_ms) tells us which *other* MCP servers the session
  connected to and whether those connections succeeded. Surfacing this turns the
  dashboard into a health signal for a user's whole MCP setup, not just ours.
- **Reliability/safety signal** — `api_error`/`api_refusal` event counts per session,
  surfaced as a simple reliability indicator (did this session hit errors or model
  refusals), not silently dropped.
- **Session cadence** — `claude_code.active_time.total` vs. wall-clock session
  duration gives an active-vs-idle split; combined with `session.count` over time,
  supports a simple usage-pattern view (sessions per day, active time trend) without
  needing anything beyond what's already emitted.
- **Context pressure as a first-class alert, not just a bar** — since `cumulative_pct`
  is already computed per block, surface a threshold-crossing indicator (e.g. session
  crossed 80% of context window) directly in the session list, not just visible after
  opening the detail view — the whole point of the Explorer is catching this before it
  bites, so don't bury the signal behind a click.

**Per-conversation / per-prompt configuration** — the telemetry itself is only
configurable at process start (env vars), not truly per-prompt from Claude Code's or
Copilot's side. The real "configuration" surface we control is dashboard-side:
- **View-level redaction toggle** — even when raw bodies were captured, let a user
  collapse/mask tool inputs-outputs and prompt text by default per session or per
  project, independent of what was captured, so a shared/demo view doesn't leak
  content just because full capture was on for debugging.
- **Per-project alert thresholds** — e.g. "flag any session over $X" or "flag context
  usage over Y%" — stored per project/owner, evaluated as sessions come in, shown as a
  badge in the session list rather than requiring the user to notice manually.
- **Retention policy per project** — auto-expire raw request/response bodies after N
  days while keeping the aggregate metrics indefinitely. Meaningful given raw bodies
  can contain full source code and conversation content — a real data-minimization
  feature, not just a nice-to-have, and directly addresses the biggest privacy
  objection to turning `OTEL_LOG_RAW_API_BODIES` on at all.
- **Manual session labeling** — Claude Code/Copilot sessions are just opaque IDs; let
  users tag/rename sessions in the dashboard ("bug-fix", "feature-x exploration") for
  later filtering — small, but turns a raw log stream into something actually
  navigable once someone has more than a handful of sessions.

None of this requires new capture beyond what's already in the confirmed schemas
(except the dashboard-side configuration items, which are pure UI + a small
per-owner/per-project settings table) — it's a matter of not discarding attributes the
mapper will already be parsing through on its way to the minimal fields the original
plan used.

### Personalized insights — 30 concrete, computable insight types

Brainstormed against the actual data model above (not generic advice) — every item
below specifies the exact aggregation and is rejected if it can't actually be computed
from stored fields. Implement as a rules engine, not ML — thresholds/comparisons over
historical per-user (and, for the cohort items, cross-user anonymized) data. Build
incrementally; this list is the backlog, not a v1 requirement.

**Cost / spend**
1. Cache-efficiency ROI by source (compare avg cost & cache-read ratio across
   `bedrock_agent`/`claude_code`/`copilot`).
2. Cost-per-session drift over time without a matching rise in turns/tool-calls.
3. Cache-creation waste on short sessions (pay for cache setup, never redeem it).
4. Model choice mismatch (expensive model used on low-complexity sessions).

**Tool usage / reliability**
5. Flaky-tool detector (per-tool failure rate vs. overall average).
6. Redundant tool-call pattern (same tool+args repeated within a session).
7. Slow-tool tax (% of total active time one tool accounts for).
8. Tool argument correlated with failure (missing a specific arg → higher error rate).
9. Unused-tool context bloat (tools listed in specs, never actually called).

**Timing / cadence**
10. Time-of-day quality effect (late-session turns/context-pressure vs. morning).
11. Day-of-week cost pattern.
12. Warm-restart waste (new session re-explains context a resume would've kept).
13. Idle-to-active ratio, correlated with slow tool calls (waiting, not thinking).

**Model / subagent / workflow choice**
14. Subagent delegation payoff — completion-rate delta, where "completion" is defined
    strictly as: session's final context_block is category `answer` vs. sessions with
    no `answer` block at all (abandoned/incomplete) — not a subjective judgment.
15. Skill/plugin effectiveness (turns/cost delta vs. a matched non-skill cohort).
16. Agent-name specialization drift — group subagent invocations by `agent.name`,
    proxy "task type" the same way item 26 does (the dominant tool_call name/signature
    within that invocation's span), and compare success-rate (per item 14's
    definition) across task-type buckets for the same agent.name.
17. MCP server reliability cost (per-server connection failure rate + startup delay).
18. Auxiliary-query overhead as % of total spend (title-gen, summarization, etc.).

**Cross-session anomaly detection**
19. Outlier-session flagging (z-score tokens/turn vs. this user's own distribution).
20. Error-rate spike detector (this week's api_error count vs. trailing average).
21. Commit-throughput regression (commits/session trending down at flat session count).
22. PR-to-session ratio drift (more exploration, less shipping, over time).

**Habit / workflow patterns**
23. Reasoning-block overspend — thinking-category tokens as % of total context, by
    model, where "no quality payoff" is operationalized narrowly and only as: no
    corresponding difference in `answer`-category block size or turn count between
    the two models compared (a rough proxy, explicitly not a real quality judgment —
    label it that way if shown to the user).
24. Cost-per-LOC-changed rising disproportionately on large-diff sessions.
25. Answer-category starvation (tool-heavy sessions that never synthesize an answer).
26. Turn-efficiency learning curve for a recurring task type (getting faster over time).
27. Front-loaded context growth (budget consumed in setup before real work starts).
28. Refusal correlation with a specific tool (api_refusal clustering near one tool).

**Anonymized cohort comparison** (cross-user aggregate, opt-in)
29. Cost-per-session vs. anonymized median for the same model+source.
30. Per-tool failure rate vs. anonymized cross-user rate for that same tool — a strong
    signal of "your environment/config" vs. "the tool is just unreliable."

## Prerequisites — repo state to resolve before starting

Two pieces of finished-but-unmerged work need to land first, cleanly, so this build
starts from one coherent `main`:

1. **`narrow-scope-bedrock-claude-code` branch** (committed, not merged) — removed
   VS Code/ChatGPT tabs, renamed "Claude Desktop" → "Claude Code" throughout,
   reworded generic "any LLM" framing. Reviewed 3x by a subagent, 91/91 tests passing.
   Merge this into `main` first (or review the diff yourself first, your call from
   last time).
2. **Dashboard/sign-out/localStorage commit** (`ec74e7b` on `main`, committed
   locally, **not pushed**) — adds the live Context Window Explorer dashboard,
   sign-out, and localStorage session persistence to the post-Authorize page. Push
   this when ready — note pushing to `main` on paths under `mcp_server/**` triggers
   `.github/workflows/deploy.yml`, which **auto-deploys to the live Cloud Run service
   and Cloudflare Worker** (`mcp-inspector.sohaibsohail.workers.dev`) — confirmed live
   and wired up despite `docs/DEPLOYMENT.md` claiming otherwise (that doc is stale,
   worth fixing during the cleanup pass below).

Build the OTLP work in a new branch off a `main` that already has both of the above.

## Build order

1. **Empirical capture** — verify Claude Code's real wire-level OTLP JSON shape
   locally (see "Unverified" note above) before writing the parser against assumed
   docs-only shape.
2. **Store layer** — incremental append functions + `source` column migration, both
   backends (SQLite + DynamoDB). Write unit tests for the new functions first —
   they're the part most likely to have subtle bugs (partial/interleaved writes,
   concurrent turns, idempotency if the same OTLP batch gets retried by the client).
3. **Generic OTLP receiver route** — auth-gated, accepts payloads, routes to a mapper
   by resource attribute, no mapper logic yet (stub/log-and-drop to prove the pipe).
4. **Claude Code mapper** — build against the real captured payload from step 1, not
   just the docs. Test with a second real local Claude Code run end-to-end: env vars
   set → dashboard shows the session appearing live.
5. **Copilot mapper** — same approach; do its own empirical capture first (VS Code +
   Copilot Chat, `COPILOT_OTEL_CAPTURE_CONTENT=true`, point at the same local
   receiver) before finalizing, same reasoning as step 1.
6. **Onboarding UX** — the two new connect-page tabs + copy snippets + the separate
   opt-in raw-bodies/content-capture toggle.
7. **Tests** — unit tests for both mappers (given a captured/fixture payload, correct
   session/turn/tool_call/context_block output), an auth-gating test for `/otlp/*`
   mirroring the existing `MultiTokenAuthMiddleware` test pattern, and an integration
   test if feasible (synthetic OTLP payload → full pipeline → dashboard API response).
8. **Real end-to-end test** — with an actual person (you, or your friend) running
   real Claude Code and/or Copilot sessions against a real deployed instance, not just
   fixtures. This is the "I'll be testing it with you" step from earlier — don't skip
   it in favor of only unit tests passing.

## Final phase — generalist cleanup + integration verification

Do this **last**, after the OTLP work is functionally complete and tested, as its own
pass over the whole repo:

1. **Generalist cleanup**: dead code, inconsistent naming, stale comments referencing
   the old "any LLM" framing that might have crept back in during this build, unused
   imports, docs that still describe the pre-OTLP architecture (`ARCHITECTURE.md`
   needs a new data-flow diagram branch for the OTLP path; `DEPLOYMENT.md`'s stale
   "GitHub Actions deploy workflow ... not built yet" line should be corrected while
   you're in there). README needs a "Claude Code / Copilot live telemetry" section —
   written with the mcpmarket.com listing in mind (clear pitch, quick setup, ideally
   a screenshot/gif of the dashboard actually populating live).
2. **Confirm the dashboard code is fully combined, not just adjacent**: the Live
   Dashboard added in `ec74e7b` must show OTLP-sourced sessions (Claude Code, Copilot)
   exactly the same way it shows Bedrock-agent sessions — same session list, same
   Context Window Explorer rendering, same metric tiles — with only the small source
   badge added per the Architecture section above as the visible difference. If any
   OTLP-specific session ever needs a different code path in the dashboard JS to
   render correctly, that's a sign the mapper isn't actually normalizing into the
   shared schema properly — fix the mapper, not the dashboard.
3. **Verify the Google auth handshake end-to-end, explicitly, one more time**: sign
   in fresh (clear localStorage first) → consent screen → Authorize → dashboard
   appears → refresh the page → still signed in (localStorage rehydration) →
   sign out → back to the sign-in button, no stale state. Do this for real in a
   browser, not just by reading the code — same rigor as the Playwright-driven check
   already done once for the dashboard. Confirm this flow is unaffected by
   everything added in this plan (it should be — nothing here touches `/auth/*` — but
   confirm, don't assume).
4. Full test suite green, one last time, on the final branch before merging to `main`.
