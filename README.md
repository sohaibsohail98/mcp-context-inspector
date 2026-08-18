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

## Auth — handing this server to other people

Two ways in, both accepted by the same `Authorization: Bearer <token>`
header on `/mcp` and every `/api/*` route:

1. **Owner token** — the `MCP_AUTH_TOKEN` above. Yours, printed on
   startup. Fine for solo local use.
2. **Google sign-in, per person** — for anyone else you want to connect
   their own LLM/agent to your server, without handing them your one
   token (and without being able to revoke just their access later).
   Set `GOOGLE_OAUTH_CLIENT_ID` (see setup below) and point them at
   `http://<your-host>:8787/auth/login` — they sign in with their own
   Google account, get a personal token minted for them
   (`mcp_server/auth_store.py`), and use that as their bearer token.
   Signing in again returns the *same* token, so pasting it into an MCP
   client config once doesn't get invalidated by a second sign-in.

**Why not a full OAuth 2.1 authorization server** (the "real" way an MCP
client is meant to discover and authenticate, per the MCP spec's OAuth
Resource Server support)? That needs a genuine authorization server —
PKCE, dynamic client registration, a consent screen, its own client/token
tables — real infrastructure disproportionate to a personal-scale
server. This gets the property that actually matters (each friend
authenticates as themselves, with their own Google account, and you can
revoke just one person) via Google Identity Services' one-tap credential
flow instead: no redirect URIs, no client secret, just a signed ID token
verified server-side (`mcp_server/google_auth.py`).

**Google Cloud setup (one-time, ~2 minutes):**
1. [console.cloud.google.com](https://console.cloud.google.com) → create
   or pick a project → **APIs & Services → Credentials**.
2. **Create Credentials → OAuth client ID** → Application type **Web
   application**.
3. Under **Authorized JavaScript origins**, add the origin(s) you'll
   serve `/auth/login` from — e.g. `http://127.0.0.1:8787` for local
   use, plus your real domain once deployed. No redirect URI needed for
   this flow.
4. Copy the **Client ID** (safe to expose client-side — it's not a
   secret) and set it as `GOOGLE_OAUTH_CLIENT_ID` in your environment
   before starting the server.

**Known limitation:** every valid token — owner or per-user — currently
sees *all* session history, not just its own; there's no per-user data
ownership in `metrics/store.py` yet. This auth model answers "can you
connect at all," not "whose data can you see." Revoke a friend's access
with `mcp_server.auth_store.revoke(google_sub)` (find their `sub` via
`list_users()`) if you need to cut someone off.

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
