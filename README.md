# mcp-context-inspector

A drop-in MCP server and execution-metrics recorder for Bedrock-based
agents and Claude Code: real per-session cost, token, and tool metrics,
plus a full Context Window Explorer, over a real MCP handshake.

<!-- TODO: capture and add a Context Window Explorer GIF here. -->

[![CI](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg)](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Why this exists

Most agent observability tools re-show data your own UI already
displayed. This one shows something you normally can't see at all:
system prompt, tool specs, reasoning, tool calls and results, and the
final answer, in the order they actually entered context, measured
against the model's real context window, with each block marked as
either visible to the end user or invisible overhead. Token counts are
honest, labeled estimates, not exact Bedrock usage. See
`docs/ARCHITECTURE.md` for why that tradeoff is the right one here.

Anthropic's Claude Code documentation page
["Explore the context window"](https://code.claude.com/docs/en/context-window),
an interactive simulation of what loads into a session's context and
what each file read costs, motivated wanting the same visibility for
an arbitrary agent loop, not just Claude Code.

## Try it in 30 seconds

Live demo: **https://mcp-inspector.sohaibsohail.workers.dev**

It's seeded with fixture data. The demo deployment runs on SQLite, so
writes made behind Google sign-in do not survive a cold start. That's
an accepted tradeoff for a free public demo, not a flaw in the storage
layer itself: a real deployment sets `STORAGE_BACKEND=firestore` or
`STORAGE_BACKEND=dynamodb` and keeps its data. See "Storage backends"
below and `docs/DEPLOYMENT.md`.

Not yet published to PyPI, so run it from source:

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

With no `MCP_AUTH_TOKEN` set, the server generates and prints one on
startup, the same trust model as a Jupyter server's printed token.

## Installation

Requires Python 3.11+.

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector
uv sync
```

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
as the bearer token for the MCP config, curl, and OTLP snippets it
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

## Usage

### Claude Code CLI

Add the server to your MCP client config with a bearer token you
already hold (the printed owner token from local dev, or a personal
token from `/auth/login` on a deployed server):

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

### Using via claude.ai chat

claude.ai's Connectors feature speaks the MCP OAuth flow directly, so
there's no token to copy for this path:

1. Go to **claude.ai → Settings → Connectors → Add custom connector**.
2. Paste the MCP server URL (`https://mcp-inspector.sohaibsohail.workers.dev/mcp`).
   Leave the OAuth Client ID and Secret fields blank; this server
   registers itself dynamically per the MCP spec.
3. claude.ai opens a Google sign-in prompt for this server automatically.
   Sign in once.
4. All 8 tools are now available in any claude.ai chat.

This connects claude.ai only. It does **not** also connect Claude Code
CLI, even though both flows use the same Google account. The two
surfaces mint and hold separate bearer tokens by design (see "Auth
model" below), so connecting Claude Code CLI needs its own trip through
`/auth/login` and its own token pasted into your MCP client config, as
described in the section above.

### Anthropic Messages API / MCP Connector

```json
{
  "mcp_servers": [{"type": "url", "url": "https://mcp-inspector.sohaibsohail.workers.dev/mcp", "name": "context-inspector", "authorization_token": "<your-token>"}],
  "tools": [{"type": "mcp_toolset", "mcp_server_name": "context-inspector"}]
}
```

This needs the `anthropic-beta: mcp-client-2025-11-20` header.

### Live telemetry: Claude Code / Copilot OTEL

Point Claude Code's or GitHub Copilot's own OpenTelemetry export at
this server and sessions show up in the dashboard as they happen: no
wrapping your agent loop, no `record_session` calls, just env vars. The
connect page generates both snippets pre-filled with your endpoint and
bearer token:

```sh
# Claude Code
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export OTEL_LOG_RAW_API_BODIES=1   # opt-in: needed for the Context Explorer

# GitHub Copilot
export COPILOT_OTEL_ENABLED=true
export OTEL_EXPORTER_OTLP_ENDPOINT=https://mcp-inspector.sohaibsohail.workers.dev/otlp
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <your-token>"
export COPILOT_OTEL_CAPTURE_CONTENT=true   # opt-in: needed for the Context Explorer
```

Sessions appear in the dashboard within the roughly 5-second log export
interval. The two `_RAW_API_BODIES`/`_CAPTURE_CONTENT` flags are
separate opt-ins because they carry full prompt and response content,
not just metrics, so the connect page keeps them behind their own
explicit toggle rather than bundling them into the base snippet. When
`OTEL_LOG_RAW_API_BODIES=1` is on, captured bodies pass through a basic
redaction layer before storage (email addresses, home-directory paths).
That's a small explicit pattern list, not comprehensive PII scrubbing;
treat it as a trust reducer, not a guarantee.

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

## Deploy your own

Cloud Run, cost-capped, demo-data or durable-storage-backed; see
`docs/DEPLOYMENT.md` for the full walkthrough.

## Auth model

Two separate token surfaces exist by design. A plain bearer token
(the owner token, or a personal token from `/auth/login`) works for
clients you can hand a token to directly, like Claude Code's MCP config
or curl. claude.ai's Connectors UI instead speaks the MCP spec's OAuth
2.1 flow and mints its own token when you sign in there, kept separate
from the plain sign-in token so disconnecting one doesn't invalidate
the other. See `docs/AUTH.md` for the full setup and threat model.
Every session is attributed to whoever recorded it: a per-user token
only ever sees, queries, and records its own data, and there is no way
to read or list another user's sessions, even by guessing an ID.

## Storage backends

Three options, selected with `STORAGE_BACKEND`. SQLite is the default:
zero setup, good for local dev, and what the public demo deployment
uses (data does not survive a cold start there, an accepted tradeoff
for a free demo). DynamoDB is a durable, AWS-hosted option for
deployments already living in AWS. Firestore is a durable, GCP-hosted
option, and the recommended choice for a real Cloud Run deployment with
sign-in-backed writes that need to survive a cold start. See
`docs/ARCHITECTURE.md` for backend-specific setup.

## License

MIT licensed; see `LICENSE`. Developed alongside
[`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI and Bedrock agent this package was extracted from.
