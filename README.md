<p align="center">
  <img src="assets/lumen-logo.svg" alt="Lumen" width="360">
</p>

<p align="center">
  A drop-in MCP server that gives Claude Code, Bedrock-based agents, and other
  MCP/LLM clients real per-session cost, token, and tool metrics — plus a full
  <strong>Context Window Explorer</strong> — over a real MCP handshake.
</p>

<p align="center">
  <a href="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml"><img src="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

Package/repo name on disk: `mcp-context-inspector`. The product is Lumen.

**[Docs site →](https://mcp-inspector.sohaibsohail.workers.dev/docs)** — same content as this
README, laid out as a proper single-page reference with section nav.

## Why this exists

Most agent observability tools re-show data your own UI already displays.
Lumen shows something you normally can't see at all: system prompt, tool
specs, reasoning, tool calls and results, and the final answer, in the order
they actually entered context — measured against the model's real context
window, with each block marked as visible-to-the-user or invisible overhead.
Token counts are honest, labeled estimates, not exact provider usage (see
[Architecture](#architecture) for why that tradeoff is the right one here).

Anthropic's Claude Code docs page
["Explore the context window"](https://code.claude.com/docs/en/context-window) —
an interactive simulation of what loads into a session and what each file read
costs — motivated wanting the same visibility for an arbitrary agent loop, not
just Claude Code.

<img src="screenshots/signin-page.png" width="380" alt="Sign-in page"> <img src="screenshots/connected-page.png" width="380" alt="Signed in: your own live dashboard">

## Try it in 30 seconds

Live demo: **https://mcp-inspector.sohaibsohail.workers.dev**

It's seeded with fixture data and runs on SQLite, so writes made behind
Google sign-in don't survive a cold start — the right tradeoff for a free
public demo, not a limitation of the storage layer (see
[Storage backends](#storage-backends)).

Not yet published to PyPI, so run it from source:

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

With no `MCP_AUTH_TOKEN` set, the server generates and prints one on startup —
same trust model as a Jupyter server's printed token.

## Installation

Requires Python 3.11+.

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector
uv sync
```

## Run it locally

```sh
cd mcp-context-inspector
uv sync

# Fixed owner token so it doesn't rotate on every restart, and a scratch
# SQLite file instead of data/metrics.db.
export MCP_AUTH_TOKEN=local-test-owner-token
export STORAGE_BACKEND=sqlite
export METRICS_DB_PATH=/tmp/mci-local-test.db
export MCP_SERVER_PORT=8787

# Optional: enables the real "Sign in with Google" button. Without it,
# /auth/login still renders and works with the owner token only — see
# "Google sign-in setup" below for how to get a client ID.
export GOOGLE_OAUTH_CLIENT_ID=<your-client-id>.apps.googleusercontent.com

uv run python -m mcp_server.server
```

Open `http://localhost:8787` (use `localhost`, not `127.0.0.1` — Google
sign-in needs it) and use `local-test-owner-token` as the bearer token for
the config it generates.

To see the dashboard populate, point a real Claude Code session's own
telemetry at it:

```sh
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_LOGS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8787/otlp \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer local-test-owner-token" \
OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code \
OTEL_LOG_RAW_API_BODIES=1 \
claude -p "say hi"
```

A session shows up within a few seconds. Run the test suite with
`uv run pytest` and lint with `uv run ruff check .`.

## Usage

### The easy way: one command

Sign in at `/auth/login` (locally, or on the live demo) and the page hands
you a single command to paste into a terminal — it writes the MCP connection
and telemetry config into your own `~/.claude/settings.json` (backed up
first, merged, never overwritten):

```sh
curl -fsSL https://mcp-inspector.sohaibsohail.workers.dev/setup/install?t=<code> | sh
```

The `?t=` code is single-use and short-lived — your real token is never in
the command itself. Not comfortable piping into a shell? The same page has a
download-then-inspect variant of the identical script. Close and reopen
Claude Code afterward (env vars only load at process startup), run one
prompt, then check "Test my connection" on the page.

### Claude Code CLI, by hand

```json
{
  "mcpServers": {
    "context-inspector": {
      "url": "https://mcp-inspector.sohaibsohail.workers.dev/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

### claude.ai chat (Connectors)

claude.ai's Connectors feature speaks the MCP OAuth flow directly — no token
to copy:

1. **claude.ai → Settings → Connectors → Add custom connector.**
2. Paste the MCP server URL (`https://mcp-inspector.sohaibsohail.workers.dev/mcp`).
   Leave OAuth Client ID/Secret blank — this server registers itself
   dynamically per the MCP spec.
3. claude.ai opens a Google sign-in prompt automatically. Sign in once.
4. All 8 tools are now available in any claude.ai chat.

This connects claude.ai only — Claude Code CLI needs its own token from
`/auth/login`, even with the same Google account (see [Auth model](#auth-model)
for why the two are separate).

Once connected, ask it things like:

- *"What did my last Claude Code session cost?"*
- *"Show me the tool-call trace for session `sess_...`"*
- *"Which of my recent sessions used the most tokens?"*
- *"What's in the system prompt block for my current session's context window?"*

### Anthropic Messages API / MCP Connector

```json
{
  "mcp_servers": [{"type": "url", "url": "https://mcp-inspector.sohaibsohail.workers.dev/mcp", "name": "context-inspector", "authorization_token": "<your-token>"}],
  "tools": [{"type": "mcp_toolset", "mcp_server_name": "context-inspector"}]
}
```

Needs the `anthropic-beta: mcp-client-2025-11-20` header.

### Live telemetry: Claude Code / Copilot OTEL, by hand

Point Claude Code's or Copilot's own OpenTelemetry export at this server and
sessions show up as they happen — no wrapping your agent loop, no
`record_session` calls, just env vars. The connect page generates both
snippets pre-filled with your endpoint and token:

```sh
# Claude Code
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_METRICS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export OTEL_RESOURCE_ATTRIBUTES=service.name=claude-code
export OTEL_METRICS_INCLUDE_SESSION_ID=true
export OTEL_LOGS_INCLUDE_SESSION_ID=true
export OTEL_LOGS_EXPORT_INTERVAL=5000
export OTEL_LOG_RAW_API_BODIES=1   # opt-in: needed for the Context Explorer

# GitHub Copilot
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export COPILOT_OTEL_CAPTURE_CONTENT=true   # opt-in: needed for the Context Explorer
```

The `OTEL_RESOURCE_ATTRIBUTES`/`*_INCLUDE_SESSION_ID` lines are what let the
server recognize the session as Claude Code's at all — omit them and every
session is silently dropped. The `_RAW_API_BODIES`/`_CAPTURE_CONTENT` flags
are separate opt-ins because they carry full prompt/response content, not
just metrics; when on, captured bodies pass through a basic redaction layer
(email addresses, home-directory paths) before storage — a trust reducer,
not comprehensive PII scrubbing.

## The 8 MCP tools

| Tool | Returns | Read/write |
|---|---|---|
| `get_session_metrics` | Session metadata + per-prompt tokens/latency/cost | Read |
| `get_token_breakdown` | Per-turn token/latency breakdown | Read |
| `get_tool_metrics` | Tool call counts by status | Read |
| `get_agent_trace` | Ordered tool-call sequence for one session | Read |
| `get_cost_estimate` | Estimated cost, one session or a time window | Read |
| `get_recent_sessions` | Most recent sessions, newest first | Read |
| `get_context_timeline` | Full context-window block breakdown | Read |
| `record_session` | Records one agent execution's metrics | Write |

Plain REST equivalents are exposed under `/api/*` — see
[Architecture](#architecture).

## Auth model

Two separate token surfaces exist by design. A plain bearer token (the owner
token, or a personal token from `/auth/login`) works for clients you hand a
token to directly — Claude Code's MCP config, curl. claude.ai's Connectors UI
instead speaks the MCP spec's OAuth 2.1 flow and mints its own token on sign-in,
kept separate so disconnecting one doesn't invalidate the other.

Every session is attributed to whoever recorded it: a per-user token only
ever sees, queries, and records its own data — there's no way to read or list
another user's sessions, even by guessing an ID. The owner token sees
everyone's (it's your server). Revoke someone's access with
`mcp_server.auth.store.revoke(google_sub)` (find their `sub` via
`list_users()`) — their existing token stops working immediately; already-
recorded data stays where it is.

### Google sign-in setup (one-time, ~2 minutes)

1. [console.cloud.google.com](https://console.cloud.google.com): create or
   pick a project → **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID** → Application type **Web
   application**.
3. Under **Authorized JavaScript origins**, add `http://localhost:8787` for
   local use, plus your real domain once deployed. Use `localhost`, not
   `127.0.0.1` — Google Identity Services only reliably honors `localhost`
   for the plain-HTTP local-dev exemption. No redirect URI needed.
4. Copy the **Client ID** (safe to expose client-side) and set it as
   `GOOGLE_OAUTH_CLIENT_ID`.

Switching `STORAGE_BACKEND` on a running deployment starts the auth store
over empty — every existing token, including your own, stops working until
you sign in again. Not a bug, just the token store being exactly as durable
as its backend.

### Letting a friend's own agent record its own data

`record_session` is also an authenticated MCP tool (and `/api/record-session`
REST route), so a friend's agent running anywhere can push its own sessions
in, attributed to them:

```python
import httpx

httpx.post(
    "http://<your-host>:8787/api/record-session",
    headers={"Authorization": f"Bearer {their_token}"},
    json={"prompt": prompt, "model_id": model_id, "loop_result": loop_result},
)
```

## Architecture

<p align="center">
  <img src="assets/architecture.svg" alt="Claude Code, browser clients, and Bedrock-style agents all talk through a Cloudflare Worker reverse proxy to a Cloud Run service, which reads and writes Firestore or DynamoDB" width="720">
</p>

The live deployment is a Cloudflare Worker (a transparent reverse proxy,
giving a short permanent public URL) in front of a Cloud Run service running
this Starlette + MCP server, backed by Firestore (or DynamoDB, or SQLite for
local dev).

```mermaid
flowchart LR
    subgraph Agent["Your agent loop"]
        A[run_agent_loop] --> R[record_session]
    end

    subgraph Package["Lumen"]
        R -->|"direct Python import\n(local, owner=None)"| S[metrics/store.py]
        MCP["mcp_server/routes/\n(MCP + REST routes)"] --> S
        S --> SQ["store_sqlite.py\n(local dev)"]
        S --> DY["store_dynamodb.py"]
        S --> FS["store_firestore.py\n(this project's own deployment)"]
    end

    subgraph Remote["Another user's own agent"]
        FR[Their run_agent_loop] -->|"record_session tool /\nPOST /api/record-session"| MCP
    end

    Client["Bedrock-based agents / Claude Code"] -->|"Streamable HTTP,\nBearer token"| MCP

    subgraph OTLPClients["Claude Code / Copilot's own telemetry"]
        CC["Claude Code"] -->|"POST /otlp/v1/logs"| OTLP
        GHC["GitHub Copilot"] -->|"POST /otlp/v1/traces"| OTLP
    end
    OTLP["mcp_server/otlp/"] -->|"append_turn / append_tool_call"| S
```

One data-access layer (`metrics/store.py`), three entry points: a direct
Python import for your own local agent, the authenticated MCP tool/REST
route for anyone else's remote agent, and `/otlp/v1/{logs,metrics,traces}`
for Claude Code's/Copilot's own native OpenTelemetry export. Every read goes
through the same layer, filtered by owner.

`record_session(prompt, model_id, loop_result, owner=None)` needs
`loop_result` shaped like:

```python
{
    "trace": [{"tool": "...", "args": {...}, "status": "ok"}, ...],
    "turns": [{"input_tokens": int, "output_tokens": int, "latency_ms": int}, ...],
    "input_tokens": int, "output_tokens": int, "total_tokens": int, "latency_ms": int,
    "context_blocks": [   # optional: omit and you just lose the Explorer, nothing crashes
        {"category": "system", "label": "...", "char_count": int, "token_estimate": int, "turn_n": int | None},
        ...
    ],
}
```

`context_blocks` categories: `system`, `tools`, `user`, `reasoning`,
`thinking`, `tool_call`, `tool_result` (optional `"status"` for color-coding
failures), `answer`.

## Storage backends

Three options, selected with `STORAGE_BACKEND`:

- **`sqlite`** (default) — zero setup, good for local dev, what the public
  demo uses. Doesn't survive a Cloud Run cold start.
- **`dynamodb`** — durable, AWS-hosted, for deployments already living in AWS.
- **`firestore`** — durable, GCP-hosted, the recommended choice for a real
  Cloud Run deployment with sign-in-backed writes. Uses
  `google.cloud.firestore.Client()` via Application Default Credentials —
  grant Cloud Run's service account `roles/datastore.user`. Needs a composite
  index on `(owner ASC, timestamp DESC)` on the `sessions` collection (create
  ahead of time via the Firestore console or `gcloud firestore indexes
  composite create` — don't rely on the first-query error link in prod).
  Test locally against the emulator: `gcloud emulators firestore start`,
  then set `FIRESTORE_EMULATOR_HOST`.

The per-user auth token store follows the same switch and needs the same
durability — a lost token silently breaks that user's auth.

## Deploying your own

Deployed to Cloud Run (`us-central1`), `--min-instances 0 --max-instances 3`,
fronted by a Cloudflare Worker reverse proxy (`cloudflare-proxy/`) for a
short public URL instead of the raw `.run.app` one. GitHub Actions (Workload
Identity Federation, no stored GCP keys) auto-deploys on push to `main` and
always routes 100% of traffic to the newly built revision.

Relevant env vars once deployed:

- `PUBLIC_ORIGIN` — the real public origin (e.g.
  `https://mcp-inspector.sohaibsohail.workers.dev`), needed because behind
  the proxy `request.base_url` reflects Cloud Run's internal origin, not what
  a real caller used. Every URL Lumen generates about itself (OAuth metadata,
  the install command) needs the real one.
- `CHAT_UI_ORIGIN` — comma-separated CORS allowlist.
- `MCP_ALLOWED_HOSTS` — comma-separated `Host` header allowlist for the MCP
  SDK's DNS-rebinding protection. Getting this wrong manifests as `421
  Invalid Host header` on authenticated requests only — an unauthenticated
  smoke test won't catch it.
- `DEV_MODE_SUBS` — comma-separated Google `sub` allowlist for developer-mode
  dashboard features (currently: showing `api_tests`' synthetic probe
  sessions, hidden from everyone else by default). Find your own `sub` via
  `mcp_server.auth.store.list_users()`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — lint, tests, and what a good PR
looks like here.

## Roadmap

- [x] Curl-pipeable one-line setup (`/setup/install`)
- [x] Owner-scoped `/otlp/debug` + "Test your connection" panel
- [x] Route-enumerating tenant-isolation test
- [ ] Windows/PowerShell installer variant
- [ ] Cursor support (OTel export + MCP config, research spike)
- [ ] Per-device token revoke
- [ ] Published to PyPI

## License

MIT licensed; see [LICENSE](LICENSE). Developed alongside
[`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI and Bedrock agent this package was extracted from.
