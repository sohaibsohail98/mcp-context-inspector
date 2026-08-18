# mcp-context-inspector

A drop-in MCP server + execution-metrics recorder for any tool-calling
agent. Point your agent's loop at `record_session(prompt, model_id,
loop_result)` after each run, and this gives you, for free:

- A real MCP server (Streamable HTTP) any MCP client can connect to —
  Claude Desktop, Cursor, your own chat UI — exposing 7 read-only tools
  over session history, cost, token/tool metrics, and...
- **The Context Window Explorer** — full transparency into exactly what
  entered the model's context window, block by block, with honest
  (explicitly-labeled-estimated) token counts, a proportional segmented
  bar, and a click-to-expand detail panel per block:

  ![Context Window Explorer](docs/context-window-explorer.png)

  *(screenshot from the reference chat UI this was built alongside —
  `sre-investigation-agent`; the panel above is what any MCP client
  gets once it queries `get_context_timeline`.)*

Most agent observability tools re-show you data your own UI already
displayed. This one shows you something you can't normally see at all:
system prompt vs. tool specs vs. reasoning vs. tool call/result vs.
final answer, in the order they actually entered context, with a
running token total against the model's real context window — and which
of those blocks are ever visible to the end user vs. invisible overhead.

## Install

```sh
uv add mcp-context-inspector   # or: pip install mcp-context-inspector
# while co-developing locally against an editable checkout:
uv add --editable ../mcp-context-inspector
```

## Wire it into your agent

```python
from metrics import store

session_id = store.record_session(prompt, model_id, loop_result)
```

`loop_result` is whatever your agent loop returns — this package only
needs it to look like:

```python
{
    "trace": [{"tool": "...", "args": {...}, "status": "ok"}, ...],
    "turns": [{"input_tokens": int, "output_tokens": int, "latency_ms": int}, ...],
    "input_tokens": int, "output_tokens": int, "total_tokens": int, "latency_ms": int,
    "context_blocks": [   # optional — omit and you just lose the Explorer, nothing crashes
        {"category": "system", "label": "...", "char_count": int, "token_estimate": int, "turn_n": int | None},
        ...
    ],
}
```

`context_blocks` categories: `system`, `tools`, `user`, `reasoning`,
`thinking`, `tool_call`, `tool_result` (optionally carries a `"status"`
key for color-coding failures), `answer`.

## Run the server

```sh
uv run python -m mcp_server.server
```

No `MCP_AUTH_TOKEN` set → generates and prints one on startup, same
trust model as a Jupyter server's printed token. Set it yourself for a
stable value across restarts. Point any MCP client at
`http://127.0.0.1:8787/mcp` with `Authorization: Bearer <token>`.

## Storage backends

`STORAGE_BACKEND=sqlite` (default, local dev — `data/metrics.db`) or
`STORAGE_BACKEND=dynamodb` (set `METRICS_TABLE`/`AWS_REGION`) — same
function signatures either way, callers never know which is active.

## The 7 MCP tools

`get_session_metrics`, `get_token_breakdown`, `get_tool_metrics`,
`get_agent_trace`, `get_cost_estimate`, `get_recent_sessions`,
`get_context_timeline`. Plain REST equivalents are also exposed under
`/api/*` — a curl-friendly debugging alternative, calling the same
underlying `metrics/store.py` functions.

## Related repos

[`sre-investigation-agent`](https://github.com/sohaibsohail98/sre-investigation-agent) —
the reference chat UI + Bedrock agent this package was extracted from
and is developed alongside.
