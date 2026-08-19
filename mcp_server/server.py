"""MCP server for the SRE agent's execution metrics — its own thing,
decoupled from agent/. Exposes 7 read-only tools plus one write tool
(record_session, for a caller's own remote agent to push its own data
in) over Streamable HTTP, plus a few plain REST routes (curl-friendly
alternative to a real MCP handshake — see web/mcp-client.js for the
actual protocol client). Both paths call the same underlying functions
in metrics/store.py — one data-access layer, not two implementations of
"how do I read a session."

Reusable, connectable by any agent — not coupled to any specific chat UI.
See web/server.py for the chat frontend, a separate process.

Run from repo root:
    uv run python -m mcp_server.server
"""

import contextvars
import os

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from mcp_server import auth_store
from mcp_server.google_auth import InvalidGoogleToken, verify_credential
from metrics import store

server = MCPServer(name="sre-agent-metrics")

# Set by MultiTokenAuthMiddleware once per request, read by every tool/REST
# handler below — the caller's Google `sub` if they connected with a
# per-user token, or None if they connected with the owner token (which
# sees everything, "it's your server" semantics). A plain contextvar
# rather than threading it through every function signature: Starlette's
# BaseHTTPMiddleware runs the downstream handler in the same asyncio
# task, so a value set here before call_next() is visible inside the
# request it wraps, including MCP tool dispatch — see
# tests/test_mcp_protocol_ownership.py for a real MCP `tools/call` test
# confirming this.
current_owner = contextvars.ContextVar("current_owner", default=None)


class MultiTokenAuthMiddleware(BaseHTTPMiddleware):
    """Gates /mcp and /api/ behind a bearer token that's either the
    owner's single shared token (`owner_token` — printed to stdout on
    startup, same "whoever can read this process's console is assumed
    to be you" model as before, kept for the maintainer's own
    zero-friction local use) OR a per-user token minted for someone who
    signed in with their own Google account via /auth/login and
    /auth/verify (`auth_store.is_valid_token`). /auth/* itself is
    deliberately unauthenticated — that's the pre-auth sign-in flow.

    Originally REST routes under /api/ were left open entirely ("no
    auth, local single-user" design) — that stopped being true the
    moment this server is meant to be handed out to multiple friends:
    those routes return the exact same session data as the MCP tools,
    so leaving them open would make the MCP-side auth pointless. Both
    paths are protected identically now.

    Also sets `current_owner` for the rest of this request — the owner
    token sees everything (current_owner stays None); a per-user token
    is resolved to its google_sub so every read/write below can filter
    to "this caller's own data only." See metrics/store.py's owner
    param and the README's Auth section."""

    def __init__(self, app, owner_token, protected_prefixes=("/mcp", "/api/")):
        super().__init__(app)
        self.owner_token = owner_token
        self.protected_prefixes = protected_prefixes

    async def dispatch(self, request, call_next):
        if any(request.url.path.startswith(p) for p in self.protected_prefixes):
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
            if token == self.owner_token:
                current_owner.set(None)
            elif token and auth_store.is_valid_token(token):
                current_owner.set(auth_store.get_sub_for_token(token))
            else:
                return JSONResponse({"error": "unauthorized — missing or wrong bearer token"}, status_code=401)
        return await call_next(request)


# --- MCP tools ---------------------------------------------------------


@server.tool()
def get_session_metrics(session_id: str) -> dict:
    """Session metadata plus per-prompt metrics (tokens, latency, cost) for one investigation."""
    return store.get_session_metrics(session_id, owner=current_owner.get()) or {"error": "session not found"}


@server.tool()
def get_token_breakdown(session_id: str) -> list:
    """Per-turn token and latency breakdown for one session."""
    return store.get_token_breakdown(session_id, owner=current_owner.get())


@server.tool()
def get_tool_metrics(session_id: str | None = None) -> list:
    """Tool call counts by status — for one session, or aggregated across all (aggregated across
    only your own sessions if you're not the server owner)."""
    return store.get_tool_metrics(session_id, owner=current_owner.get())


@server.tool()
def get_agent_trace(session_id: str) -> list:
    """The ordered sequence of tool calls (name, args, status) for one session."""
    return store.get_agent_trace(session_id, owner=current_owner.get())


@server.tool()
def get_cost_estimate(session_id: str | None = None, period_seconds: int | None = None) -> float:
    """Estimated cost for one session, or summed over the last period_seconds (your own
    sessions only, unless you're the server owner)."""
    return store.get_cost_estimate(session_id, period_seconds, owner=current_owner.get())


@server.tool()
def get_recent_sessions(limit: int = 10) -> list:
    """The most recent investigation sessions, newest first — your own only, unless
    you're the server owner (owner token), who sees everyone's."""
    return store.get_recent_sessions(limit, owner=current_owner.get())


@server.tool()
def get_context_timeline(session_id: str) -> list:
    """Ordered, categorized breakdown of everything that entered this
    session's context window, with cumulative token estimates —
    character-based estimates for composition, not exact Bedrock usage
    numbers (see get_session_metrics for those)."""
    return store.get_context_timeline(session_id, owner=current_owner.get())


@server.tool()
def record_session(prompt: str, model_id: str, loop_result: dict) -> str:
    """Records one agent execution's metrics, attributed to whoever's
    connected (your own Google account, if you signed in via
    /auth/login — the owner token records as owner=None, same as the
    local direct-import path `from metrics.store import record_session`
    used by this server's own reference agent). This is how a friend's
    own remote agent gets its sessions into this server at all, rather
    than only being able to query data the server owner recorded
    locally. loop_result must have the shape agent/runtime.py's
    run_agent_loop() returns — see this package's README for the exact
    contract. Returns the new session_id."""
    return store.record_session(prompt, model_id, loop_result, owner=current_owner.get())


# --- REST routes — documented curl-debugging alternative to the real MCP
# protocol handshake (same underlying functions as the tools above) -----


@server.custom_route("/api/sessions", methods=["GET"])
async def api_sessions(request: Request):
    limit = int(request.query_params.get("limit", 10))
    return JSONResponse(store.get_recent_sessions(limit, owner=current_owner.get()))


@server.custom_route("/api/sessions/{session_id}", methods=["GET"])
async def api_session_detail(request: Request):
    session_id = request.path_params["session_id"]
    owner = current_owner.get()
    metrics = store.get_session_metrics(session_id, owner=owner)
    if metrics is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "metrics": metrics,
            "turns": store.get_token_breakdown(session_id, owner=owner),
            "trace": store.get_agent_trace(session_id, owner=owner),
        }
    )


@server.custom_route("/api/tool-metrics", methods=["GET"])
async def api_tool_metrics(request: Request):
    return JSONResponse(store.get_tool_metrics(owner=current_owner.get()))


@server.custom_route("/api/cost", methods=["GET"])
async def api_cost(request: Request):
    period = request.query_params.get("period_seconds")
    period = int(period) if period else None
    return JSONResponse({"total_cost": store.get_cost_estimate(period_seconds=period, owner=current_owner.get())})


@server.custom_route("/api/context-timeline/{session_id}", methods=["GET"])
async def api_context_timeline(request: Request):
    session_id = request.path_params["session_id"]
    return JSONResponse(store.get_context_timeline(session_id, owner=current_owner.get()))


@server.custom_route("/api/record-session", methods=["POST"])
async def api_record_session(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        prompt, model_id, loop_result = body["prompt"], body["model_id"], body["loop_result"]
    except KeyError as e:
        return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
    session_id = store.record_session(prompt, model_id, loop_result, owner=current_owner.get())
    return JSONResponse({"session_id": session_id})


# --- Google sign-in — the pre-auth flow that mints a per-user MCP token.
# Deliberately unauthenticated (that's the point) and deliberately not
# gated by MultiTokenAuthMiddleware (which only checks /mcp and /api/).


_PAGE_STYLE = """
  :root {
    --bg: #0b0f14; --bg-raised: #121822; --border: #223044;
    --text: #d8e2ee; --text-dim: #8296ac;
    --accent: #2dd8c4; --accent-dim: #1a5f57;
    --warn: #f5b955; --warn-dim: #4a3a1a;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    max-width: 680px; margin: 3rem auto; padding: 0 1.5rem;
    background: var(--bg); color: var(--text); line-height: 1.55;
  }
  h1 { font-size: 1.5rem; margin-bottom: 0.2rem; letter-spacing: -0.01em; }
  h1::before { content: "◈ "; color: var(--accent); }
  h3 { margin-top: 0; font-size: 0.95rem; color: var(--text); }
  .sub { color: var(--text-dim); margin-top: 0; margin-bottom: 1.75rem; font-size: 0.95rem; }
  .card {
    border: 1px solid var(--border); background: var(--bg-raised);
    border-radius: 12px; padding: 1.25rem 1.5rem; margin: 1.1rem 0;
  }
  .card.security { border-color: var(--warn-dim); }
  ul.features { padding-left: 1.2rem; margin-bottom: 0; }
  ul.features li { margin-bottom: 0.5rem; }
  ul.features li::marker { color: var(--accent); }
  code, pre {
    background: #060a0f; border: 1px solid var(--border); border-radius: 6px;
    color: #a8f0e4; font-family: ui-monospace, "SF Mono", Menlo, monospace;
  }
  code { padding: 0.15rem 0.45rem; font-size: 0.85em; border-width: 0; background: #0e1620; }
  pre { padding: 1rem; overflow-x: auto; font-size: 0.8rem; white-space: pre-wrap; word-break: break-all; }
  a { color: var(--accent); }
  button.copy {
    font-size: 0.75rem; padding: 0.35rem 0.7rem; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg); color: var(--text);
    cursor: pointer; margin-left: 0.4rem; margin-top: 0.5rem;
  }
  button.copy:hover { border-color: var(--accent); color: var(--accent); }
  .hidden { display: none; }
  details {
    border: 1px solid var(--border); border-radius: 10px; padding: 0.7rem 1rem;
    margin: 0.6rem 0; background: var(--bg-raised);
  }
  details summary {
    cursor: pointer; font-size: 0.88rem; color: var(--text-dim);
    list-style: none;
  }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "▸  "; color: var(--accent); }
  details[open] summary::before { content: "▾  "; }
  details p { margin-bottom: 0; margin-top: 0.6rem; font-size: 0.85rem; color: var(--text-dim); }
  .badge {
    display: inline-block; font-size: 0.7rem; padding: 0.15rem 0.5rem; border-radius: 999px;
    background: var(--accent-dim); color: var(--accent); margin-left: 0.4rem; vertical-align: middle;
  }
"""


@server.custom_route("/auth/login", methods=["GET"])
async def auth_login(request: Request):
    intro = """
<h1>mcp-context-inspector</h1>
<p class="sub">Execution metrics + a full Context Window Explorer for any tool-calling agent, over a real MCP server.</p>
<div class="card">
  <h3>What this gives your LLM/agent</h3>
  <ul class="features">
    <li>Real per-session cost, token, and tool-call metrics — 7 read-only MCP tools</li>
    <li>The <strong>Context Window Explorer</strong> — exactly what entered the model's context window, block by block, with honest token estimates</li>
    <li>Your own data, isolated from anyone else connected to this server — sign in below and everything you record or query is scoped to your account</li>
  </ul>
</div>
<details>
  <summary>What is an MCP server?</summary>
  <p>MCP (Model Context Protocol) is an open standard that lets an LLM or agent call tools over a
  normal HTTP connection — the same handshake works whether you're connecting Claude, ChatGPT,
  Cursor, or your own custom agent. This server exposes read/write tools for agent execution
  data; nothing here is specific to any one AI provider.</p>
</details>
<details>
  <summary>Why sign in with Google instead of a password?</summary>
  <p>No account to create or password to remember here — Google verifies who you are, this
  server just checks the signed proof and hands you a token scoped to your account. That token,
  not your Google identity itself, is what your agent actually uses afterward.</p>
</details>
<details>
  <summary>What does "your own data" actually mean?</summary>
  <p>Every session recorded through your token is tagged with your account. Reads are filtered
  the same way — you only ever see, list, or query sessions you recorded. Even guessing another
  person's session ID reads back as "not found," identical to one that never existed.</p>
</details>
"""
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return HTMLResponse(f"""<!doctype html>
<html><head><title>mcp-context-inspector</title><style>{_PAGE_STYLE}</style></head>
<body>{intro}
<div class="card security">
  <p>Google sign-in isn't configured on this server — <code>GOOGLE_OAUTH_CLIENT_ID</code>
  isn't set. Ask whoever's running it to set that up, or use the owner's shared token instead.</p>
</div>
</body></html>""", status_code=503)

    return HTMLResponse(f"""<!doctype html>
<html><head><title>mcp-context-inspector</title>
<style>{_PAGE_STYLE}</style>
<script src="https://accounts.google.com/gsi/client" async defer></script>
</head><body>
<div id="intro">{intro}
<div class="card">
  <h3>Sign in to get your token</h3>
  <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">One click — no password, no account to create here.</p>
  <div id="g_id_onload" data-client_id="{client_id}" data-callback="onSignIn"></div>
  <div class="g_id_signin" data-type="standard" data-theme="filled_black"></div>
</div>
</div>
<div id="landing" class="hidden"></div>
<script>
  const mcpUrl = window.location.origin + "/mcp";

  function connectPage(email, token) {{
    const claudeConfig = JSON.stringify({{
      mcpServers: {{
        "context-inspector": {{
          url: mcpUrl,
          headers: {{ Authorization: "Bearer " + token }}
        }}
      }}
    }}, null, 2);
    return `
      <h1>You're connected<span class="badge">` + email + `</span></h1>
      <p class="sub">Everything below is scoped to your account only.</p>
      <div class="card security">
        <h3>Your token</h3>
        <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">Keep this private — it's what identifies your data on this server. Anyone with it can read and record data as you.</p>
        <pre id="token-box">` + token + `</pre>
        <button class="copy" onclick="copyText('token-box')">Copy token</button>
      </div>
      <div class="card">
        <h3>MCP server URL</h3>
        <pre>` + mcpUrl + `</pre>
      </div>
      <div class="card">
        <h3>Claude Desktop / any MCP-config-based client</h3>
        <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">Add this to your client's MCP server config:</p>
        <pre id="claude-config">` + claudeConfig + `</pre>
        <button class="copy" onclick="copyText('claude-config')">Copy config</button>
      </div>
      <div class="card">
        <h3>Any other LLM/agent (raw header)</h3>
        <p style="margin-top:0; color: var(--text-dim); font-size: 0.9rem;">Point it at the MCP server URL above with this header on every request:</p>
        <pre>Authorization: Bearer ` + token + `</pre>
      </div>
      <div class="card">
        <h3>curl (debugging)</h3>
        <pre>curl -H "Authorization: Bearer ` + token + `" ` + window.location.origin + `/api/sessions</pre>
      </div>
      <details>
        <summary>How do I record my own agent's sessions here, not just read?</summary>
        <p>Call the <code>record_session</code> MCP tool (or POST <code>/api/record-session</code>) with the same
        bearer token — whatever you record is automatically attributed to you, the same way reads are scoped.
        See the package README's "Letting a friend's own agent record its own data" section for the exact shape.</p>
      </details>
      <details>
        <summary>Can this token be revoked?</summary>
        <p>Yes — the server owner can revoke your access at any time; you'd just sign in again here for a new one.
        Your already-recorded data isn't deleted, and stays visible only to you and the server owner.</p>
      </details>
    `;
  }}

  function copyText(id) {{
    navigator.clipboard.writeText(document.getElementById(id).textContent);
  }}

  async function onSignIn(response) {{
    const res = await fetch("/auth/verify", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{credential: response.credential}}),
    }});
    const data = await res.json();
    if (res.ok) {{
      document.getElementById("intro").classList.add("hidden");
      const landing = document.getElementById("landing");
      landing.classList.remove("hidden");
      landing.innerHTML = connectPage(data.email, data.mcp_token);
    }} else {{
      document.getElementById("landing").classList.remove("hidden");
      document.getElementById("landing").innerHTML = "<div class='card security'>Sign-in failed: " + (data.error || "unknown error") + "</div>";
    }}
  }}
</script>
</body></html>""")


@server.custom_route("/auth/verify", methods=["POST"])
async def auth_verify(request: Request):
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return JSONResponse({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"}, status_code=503)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    credential = body.get("credential")
    if not credential:
        return JSONResponse({"error": "missing credential"}, status_code=400)

    try:
        identity = verify_credential(credential, client_id)
    except InvalidGoogleToken as e:
        return JSONResponse({"error": f"invalid Google credential: {e}"}, status_code=401)

    token = auth_store.get_or_create_token(identity["sub"], identity["email"])
    return JSONResponse({"mcp_token": token, "email": identity["email"]})


if __name__ == "__main__":
    import secrets

    import uvicorn

    # No token configured → generate one for this run and print it,
    # rather than starting the /mcp endpoint wide open. Set MCP_AUTH_TOKEN
    # yourself for a stable value across restarts (e.g. so a browser tab
    # left open doesn't need re-pasting every time you restart the server).
    # This is the OWNER's token — friends should sign in via /auth/login
    # instead of being handed this one, so revoking a single friend's
    # access doesn't mean rotating everyone's token.
    mcp_auth_token = os.environ.get("MCP_AUTH_TOKEN")
    if not mcp_auth_token:
        mcp_auth_token = secrets.token_urlsafe(24)
        print(
            "\n─── No MCP_AUTH_TOKEN set — generated one for this run "
            + "─" * 10
            + f"\n\n    {mcp_auth_token}\n\n"
            "This is YOUR (owner) token — paste it into the chat UI's MCP "
            "connect panel to authenticate.\n"
        )

    server_port = int(os.environ.get("MCP_SERVER_PORT", "8787"))
    if os.environ.get("GOOGLE_OAUTH_CLIENT_ID"):
        print(f"Google sign-in enabled — friends can get their own token at http://127.0.0.1:{server_port}/auth/login\n")
    else:
        print("GOOGLE_OAUTH_CLIENT_ID not set — /auth/login will report sign-in as unavailable; only the owner token above works.\n")

    # Built with server.run(transport="streamable-http") — but that path
    # doesn't expose the underlying Starlette app for CORS configuration,
    # and the chat panel's real MCP handshake (web/mcp-client.js, running
    # from web.server's own origin/port) needs cross-origin access to
    # /mcp. Constructing the app via streamable_http_app() ourselves and
    # wrapping it in CORSMiddleware is the same server, just runnable
    # with the middleware attached.
    web_port = int(os.environ.get("WEB_SERVER_PORT", "8788"))
    http_app = server.streamable_http_app()
    # Auth middleware added before CORS so CORS ends up outermost (Starlette
    # runs the most-recently-added middleware first) — CORS preflight
    # (OPTIONS, no Authorization header) gets handled and answered before
    # ever reaching the auth check, and CORS headers still land on 401s.
    http_app.add_middleware(MultiTokenAuthMiddleware, owner_token=mcp_auth_token)
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://127.0.0.1:{web_port}", f"http://localhost:{web_port}"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Accept", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )
    # Port overridable so tests can boot a throwaway instance without
    # colliding with a real dev.server session already running on the
    # default port.
    uvicorn.run(http_app, host="127.0.0.1", port=server_port)
