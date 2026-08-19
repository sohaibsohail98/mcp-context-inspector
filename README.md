# mcp-context-inspector

A drop-in MCP server + execution-metrics recorder for any tool-calling
agent — real per-session cost/token/tool metrics, and a full Context
Window Explorer, over a real MCP handshake.

<!-- Context Window Explorer GIF — capturing it is separate, later work. -->
<img src="docs/screenshots/context-window-explorer.gif" width="640" alt="Context Window Explorer">

[![CI](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg)](https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

## Why this exists

Most agent observability tools re-show you data your own UI already
displayed. This one shows you something you normally can't see at all:
system prompt vs. tool specs vs. reasoning vs. tool call/result vs.
final answer, in the order they actually entered context, against the
model's real context window — and which blocks are ever visible to the
end user vs. invisible overhead. Token counts are honest, labeled
estimates, not exact Bedrock usage — see `docs/ARCHITECTURE.md` for why
that's the right tradeoff here, not a limitation.

Anthropic's Claude Code documentation page
["Explore the context window"](https://code.claude.com/docs/en/context-window)
— an interactive simulation of what loads into a session's context and
what each file read costs — is what motivated wanting the same
visibility for an arbitrary agent loop, not just Claude Code.

## Try it in 30 seconds

Live demo: **https://mcp-inspector.sohaibsohail.workers.dev**
(seeded with fixture data, writes behind Google sign-in reset on cold
start — see `docs/DEPLOYMENT.md`).

Not yet published to PyPI — run it from source:

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

No `MCP_AUTH_TOKEN` set → generates and prints one on startup, same
trust model as a Jupyter server's printed token.

## Connect your client

Claude Desktop / any MCP-config-based client:
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

VS Code (`.vscode/mcp.json`):
```json
{
  "servers": {
    "context-inspector": {
      "type": "http",
      "url": "https://mcp-inspector.sohaibsohail.workers.dev/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```

Claude.ai / ChatGPT (developer mode): in-app "add custom connector"
flow — paste the `/mcp` URL above, no config file.

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

Plain REST equivalents are also exposed under `/api/*` — see
`docs/ARCHITECTURE.md`.

## Deploy your own

Cloud Run, cost-capped, demo-data or DynamoDB-backed — see
`docs/DEPLOYMENT.md`.

## Auth

Owner token for solo use, or per-person Google sign-in so friends can
connect their own agent without sharing your token — each person's data
stays isolated to them. See `docs/AUTH.md` for setup and the isolation
guarantee.

## Storage backends & related repo

SQLite (local dev) or DynamoDB (deployed) — see `docs/ARCHITECTURE.md`.
Developed alongside [`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI + Bedrock agent this package was extracted from.
MIT licensed — see `LICENSE`.
