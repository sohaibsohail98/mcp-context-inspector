# mcp-context-inspector

A drop-in MCP server + execution-metrics recorder for Bedrock-based
agents and Claude Code: real per-session cost/token/tool metrics, and
a full Context Window Explorer, over a real MCP handshake.

<!-- Context Window Explorer GIF (capturing it is separate, later work). -->
<img src="docs/screenshots/context-window-explorer.gif" width="640" alt="Context Window Explorer">

[![CI](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg)](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Why this exists

Most agent observability tools re-show you data your own UI already
displayed. This one shows you something you normally can't see at all:
system prompt vs. tool specs vs. reasoning vs. tool call/result vs.
final answer, in the order they actually entered context, against the
model's real context window, and which blocks are ever visible to the
end user vs. invisible overhead. Token counts are honest, labeled
estimates, not exact Bedrock usage; see `docs/ARCHITECTURE.md` for why
that's the right tradeoff here, not a limitation.

Anthropic's Claude Code documentation page
["Explore the context window"](https://code.claude.com/docs/en/context-window),
an interactive simulation of what loads into a session's context and
what each file read costs, is what motivated wanting the same
visibility for an arbitrary agent loop, not just Claude Code.

## Try it in 30 seconds

Live demo: **https://mcp-inspector.sohaibsohail.workers.dev**
(seeded with fixture data, writes behind Google sign-in reset on cold
start; see `docs/DEPLOYMENT.md`).

Not yet published to PyPI, run it from source:

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

No `MCP_AUTH_TOKEN` set, so it generates and prints one on startup, same
trust model as a Jupyter server's printed token.

## Run it locally

The full local dev loop, useful for testing changes before they hit the
live demo:

```sh
cd mcp-context-inspector
uv sync

# Pick a fixed owner token so it doesn't rotate on every restart, and
# point writes at a scratch SQLite file instead of data/metrics.db.
export MCP_AUTH_TOKEN=local-test-owner-token
export STORAGE_BACKEND=sqlite
export METRICS_DB_PATH=/tmp/mci-local-test.db
export MCP_SERVER_PORT=8787

# Optional: enables the real "Sign in with Google" button. Without it,
# /auth/login still renders and works, just with the owner token only
# (see docs/AUTH.md's "Google Cloud setup" for how to get a client ID;
# make sure http://localhost:8787 is in that client's authorized
# JavaScript origins).
export GOOGLE_OAUTH_CLIENT_ID=<your-client-id>.apps.googleusercontent.com

uv run python -m mcp_server.server
```

Then open `http://localhost:8787` in a browser (use `localhost`, not
`127.0.0.1`, for Google sign-in to work) and use `local-test-owner-token`
as the bearer token for the MCP config / curl / OTLP snippets it
generates.

To see the dashboard actually populate, point a real Claude Code
session's own telemetry at it:

```sh
CLAUDE_CODE_ENABLE_TELEMETRY=1 \
OTEL_LOGS_EXPORTER=otlp \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:8787/otlp \
OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer local-test-owner-token" \
OTEL_LOG_RAW_API_BODIES=1 \
claude -p "say hi"
```

A session should show up in the dashboard within a few seconds. Run the
test suite with `uv run pytest`.

## Connect your client

Claude Code:
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

Anthropic Messages API (MCP connector, beta):
```json
{
  "mcp_servers": [{"type": "url", "url": "https://mcp-inspector.sohaibsohail.workers.dev/mcp", "name": "context-inspector", "authorization_token": "<your-token>"}],
  "tools": [{"type": "mcp_toolset", "mcp_server_name": "context-inspector"}]
}
```
(needs the `anthropic-beta: mcp-client-2025-11-20` header.)

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

Plain REST equivalents are also exposed under `/api/*`; see
`docs/ARCHITECTURE.md`.

## Claude Code / Copilot live telemetry

Point Claude Code's or GitHub Copilot's own OpenTelemetry export at this
server and sessions show up in the dashboard as they happen: no
wrapping your agent loop, no `record_session` calls, just env vars.
The connect page generates both snippets pre-filled with your endpoint
and bearer token:

```sh
# Claude Code
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export OTEL_LOG_RAW_API_BODIES=1   # opt-in: needed for the Context Explorer

# GitHub Copilot
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export COPILOT_OTEL_CAPTURE_CONTENT=true   # opt-in: needed for the Context Explorer
```

Sessions appear in the dashboard within the ~5s log export interval.
The two `_RAW_API_BODIES`/`_CAPTURE_CONTENT` flags are separate opt-ins
because they carry full prompt/response content, not just metrics; the
connect page keeps them behind their own explicit toggle rather than
bundling them into the base snippet.

## Deploy your own

Cloud Run, cost-capped, demo-data or DynamoDB-backed; see
`docs/DEPLOYMENT.md`.

## Auth

Owner token for solo use, or per-person Google sign-in so friends can
connect their own agent without sharing your token, each person's data
stays isolated to them. See `docs/AUTH.md` for setup and the isolation
guarantee.

## Storage backends & related repo

SQLite (local dev) or DynamoDB (deployed); see `docs/ARCHITECTURE.md`.
Developed alongside [`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI + Bedrock agent this package was extracted from.
MIT licensed; see `LICENSE`.
