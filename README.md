<p align="center">
  <img src="assets/ctxwindow-logo.svg" alt="CtxWindow" width="360">
</p>

<p align="center">
  A drop-in MCP server that gives Claude Code, Bedrock-based agents, and other
  MCP/LLM clients real per-session cost, token, and tool metrics, plus a full
  <strong>Context Window Explorer</strong>, over a real MCP handshake.
</p>

<p align="center">
  <a href="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml"><img src="https://github.com/sohaibsohail98/mcp-context-inspector/actions/workflows/tests.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green" alt="License: MIT"></a>
</p>

![Typing a prompt into Claude Code, then switching to the live ctxwindow dashboard and opening that session's Context Window Explorer blocks](docs/demo.gif)

> **Independent, unaffiliated open-source project.** ctxwindow is not built, maintained, or
> endorsed by Anthropic. "Claude" and "Claude Code" are Anthropic's products; ctxwindow
> reads their publicly documented OpenTelemetry export and MCP protocol, nothing more.

The package/repo name on disk stays `mcp-context-inspector`; the product it ships is
called **ctxwindow** (after its domain, [ctxwindow.uk](https://ctxwindow.uk)).

## Quick start

Live demo, no install: **https://ctxwindow.uk**

Not yet published to PyPI, so run it from source (Python 3.11+):

```sh
git clone https://github.com/sohaibsohail98/mcp-context-inspector
cd mcp-context-inspector && uv run python -m mcp_server.server
```

With no `MCP_AUTH_TOKEN` set, the server generates and prints one on startup, using the
same trust model as a Jupyter server's printed token.

Then sign in at `/auth/login` (locally or on the live demo) and the page hands you one
command that writes the MCP connection and telemetry config into your own
`~/.claude/settings.json` (backed up first, merged, never overwritten):

```sh
curl -fsSL https://ctxwindow.uk/setup/install?t=<code> | sh
```

The `?t=` code is single-use and short-lived, so your real token is never in the command
itself. Close and reopen Claude Code afterward, since env vars only load at process
startup, then run one prompt and check "Test my connection" on the page.

Prefer to wire it up by hand, or connect claude.ai, the Messages API, or Copilot instead?
See [Usage](https://ctxwindow.uk/docs#one-command) and
[Run it locally](https://ctxwindow.uk/docs#run-locally).

## Documentation

- [Docs site](https://ctxwindow.uk/docs), the full single-page reference
  ([architecture](https://ctxwindow.uk/docs#architecture),
  [auth model](https://ctxwindow.uk/docs#auth-model),
  [storage backends](https://ctxwindow.uk/docs#storage-backends),
  [deploying your own](https://ctxwindow.uk/docs#deploying),
  [environment variables](https://ctxwindow.uk/docs#env-vars),
  [roadmap](https://ctxwindow.uk/docs#roadmap))
- [CONTRIBUTING.md](CONTRIBUTING.md), lint, tests, and what a good PR looks like here
- [LICENSE](LICENSE), MIT
- [Report a bug](https://github.com/sohaibsohail98/mcp-context-inspector/issues) or
  [ask a question](https://github.com/sohaibsohail98/mcp-context-inspector/issues/new?title=ctxwindow%3A%20question&labels=question).
  For a security issue, please open a
  [private security advisory](https://github.com/sohaibsohail98/mcp-context-inspector/security/advisories/new)
  instead of a public issue.

## Why this exists

Most agent observability tools re-show data your own UI already displays. ctxwindow shows
something you normally can't see at all: system prompt, tool specs, reasoning, tool calls
and results, and the final answer, in the order they actually entered context. Each block
is measured against the model's real context window and marked as either visible to the
user or invisible overhead. Token counts are honest, labeled estimates, not exact provider
usage (see [Architecture](https://ctxwindow.uk/docs#architecture) for why that tradeoff is
the right one here).

Anthropic's Claude Code docs page,
["Explore the context window"](https://code.claude.com/docs/en/context-window), is an
interactive simulation of what loads into a session and what each file read costs. It
motivated wanting the same visibility for an arbitrary agent loop, not just Claude Code.

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

Plain REST equivalents are exposed under `/api/*`. Payload shapes are in
[Architecture](https://ctxwindow.uk/docs#architecture).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for lint, tests, and what a good PR looks like
here. Run the suite with `uv run pytest` and lint with `uv run ruff check .`.

## License

MIT licensed; see [LICENSE](LICENSE). Developed alongside
[`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent),
the reference chat UI and Bedrock agent this package was extracted from.
