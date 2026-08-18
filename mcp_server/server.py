"""MCP server for the SRE agent's execution metrics — its own thing,
decoupled from agent/. Exposes 7 read-only tools over Streamable HTTP,
plus a few plain REST routes (curl-friendly alternative to a real MCP
handshake — see web/mcp-client.js for the actual protocol client). Both
paths call the same underlying functions in metrics/store.py — one
data-access layer, not two implementations of "how do I read a session."

Reusable, connectable by any agent — not coupled to any specific chat UI.
See web/server.py for the chat frontend, a separate process.

Run from repo root:
    uv run python -m mcp_server.server
"""

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

    Known limitation, not solved here: every valid token (owner or
    per-user) currently sees ALL session history, not just its own —
    there's no per-user data ownership/scoping in metrics/store.py yet.
    This middleware answers "can you connect at all," not "whose data
    can you see." See the README's Auth section."""

    def __init__(self, app, owner_token, protected_prefixes=("/mcp", "/api/")):
        super().__init__(app)
        self.owner_token = owner_token
        self.protected_prefixes = protected_prefixes

    async def dispatch(self, request, call_next):
        if any(request.url.path.startswith(p) for p in self.protected_prefixes):
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
            if token != self.owner_token and not (token and auth_store.is_valid_token(token)):
                return JSONResponse({"error": "unauthorized — missing or wrong bearer token"}, status_code=401)
        return await call_next(request)


# --- MCP tools ---------------------------------------------------------


@server.tool()
def get_session_metrics(session_id: str) -> dict:
    """Session metadata plus per-prompt metrics (tokens, latency, cost) for one investigation."""
    return store.get_session_metrics(session_id) or {"error": "session not found"}


@server.tool()
def get_token_breakdown(session_id: str) -> list:
    """Per-turn token and latency breakdown for one session."""
    return store.get_token_breakdown(session_id)


@server.tool()
def get_tool_metrics(session_id: str | None = None) -> list:
    """Tool call counts by status — for one session, or aggregated across all."""
    return store.get_tool_metrics(session_id)


@server.tool()
def get_agent_trace(session_id: str) -> list:
    """The ordered sequence of tool calls (name, args, status) for one session."""
    return store.get_agent_trace(session_id)


@server.tool()
def get_cost_estimate(session_id: str | None = None, period_seconds: int | None = None) -> float:
    """Estimated cost for one session, or summed over the last period_seconds."""
    return store.get_cost_estimate(session_id, period_seconds)


@server.tool()
def get_recent_sessions(limit: int = 10) -> list:
    """The most recent investigation sessions, newest first."""
    return store.get_recent_sessions(limit)


@server.tool()
def get_context_timeline(session_id: str) -> list:
    """Ordered, categorized breakdown of everything that entered this
    session's context window, with cumulative token estimates —
    character-based estimates for composition, not exact Bedrock usage
    numbers (see get_session_metrics for those)."""
    return store.get_context_timeline(session_id)


# --- REST routes — documented curl-debugging alternative to the real MCP
# protocol handshake (same underlying functions as the tools above) -----


@server.custom_route("/api/sessions", methods=["GET"])
async def api_sessions(request: Request):
    limit = int(request.query_params.get("limit", 10))
    return JSONResponse(store.get_recent_sessions(limit))


@server.custom_route("/api/sessions/{session_id}", methods=["GET"])
async def api_session_detail(request: Request):
    session_id = request.path_params["session_id"]
    metrics = store.get_session_metrics(session_id)
    if metrics is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "metrics": metrics,
            "turns": store.get_token_breakdown(session_id),
            "trace": store.get_agent_trace(session_id),
        }
    )


@server.custom_route("/api/tool-metrics", methods=["GET"])
async def api_tool_metrics(request: Request):
    return JSONResponse(store.get_tool_metrics())


@server.custom_route("/api/cost", methods=["GET"])
async def api_cost(request: Request):
    period = request.query_params.get("period_seconds")
    period = int(period) if period else None
    return JSONResponse({"total_cost": store.get_cost_estimate(period_seconds=period)})


@server.custom_route("/api/context-timeline/{session_id}", methods=["GET"])
async def api_context_timeline(request: Request):
    session_id = request.path_params["session_id"]
    return JSONResponse(store.get_context_timeline(session_id))


# --- Google sign-in — the pre-auth flow that mints a per-user MCP token.
# Deliberately unauthenticated (that's the point) and deliberately not
# gated by MultiTokenAuthMiddleware (which only checks /mcp and /api/).


@server.custom_route("/auth/login", methods=["GET"])
async def auth_login(request: Request):
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return HTMLResponse(
            "<p>Google sign-in isn't configured on this server — "
            "GOOGLE_OAUTH_CLIENT_ID isn't set. Ask whoever's running it "
            "to set that up, or use the owner's shared token instead.</p>",
            status_code=503,
        )
    return HTMLResponse(f"""<!doctype html>
<html><head><title>Connect to mcp-context-inspector</title>
<script src="https://accounts.google.com/gsi/client" async defer></script>
</head><body style="font-family: sans-serif; max-width: 480px; margin: 4rem auto;">
<h2>Sign in to get your MCP token</h2>
<p>Sign in with Google to get a personal token for connecting your LLM/agent to this MCP server.</p>
<div id="g_id_onload"
     data-client_id="{client_id}"
     data-callback="onSignIn">
</div>
<div class="g_id_signin" data-type="standard"></div>
<pre id="result" style="white-space: pre-wrap; background: #f4f4f4; padding: 1rem; display: none;"></pre>
<script>
  async function onSignIn(response) {{
    const res = await fetch("/auth/verify", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{credential: response.credential}}),
    }});
    const data = await res.json();
    const out = document.getElementById("result");
    out.style.display = "block";
    if (res.ok) {{
      out.textContent = "Signed in as " + data.email + "\\n\\nYour MCP token:\\n" + data.mcp_token +
        "\\n\\nUse this as: Authorization: Bearer <token>";
    }} else {{
      out.textContent = "Sign-in failed: " + (data.error || "unknown error");
    }}
  }}
</script>
</body></html>""")


@server.custom_route("/auth/verify", methods=["POST"])
async def auth_verify(request: Request):
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        return JSONResponse({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"}, status_code=503)

    body = await request.json()
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
