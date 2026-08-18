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

from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from metrics import store

server = MCPServer(name="sre-agent-metrics")


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Shared-secret gate on the MCP protocol path only — not the MCP
    SDK's own OAuth Resource Server support (MCPServer's `auth=`
    parameter), which requires a real `issuer_url`: an actual OAuth/OIDC
    authorization server (AWS Cognito, Auth0, a self-hosted one) has to
    exist somewhere for that machinery to mean anything. Standing one up
    is genuine infrastructure, disproportionate to gating a single-user
    local dev server. This gives the same practical property — an
    unauthenticated request can't connect — the same way a Jupyter
    server's printed token does: whoever can read this process's stdout
    is assumed to be you, at zero setup cost. REST routes under /api/
    stay open, matching this project's existing "no auth, local
    single-user" design for those (see docs/PROJECT.md)."""

    def __init__(self, app, token, protected_path="/mcp"):
        super().__init__(app)
        self.token = token
        self.protected_path = protected_path

    async def dispatch(self, request, call_next):
        if request.url.path.startswith(self.protected_path):
            if request.headers.get("authorization") != f"Bearer {self.token}":
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


if __name__ == "__main__":
    import os
    import secrets

    import uvicorn

    # No token configured → generate one for this run and print it,
    # rather than starting the /mcp endpoint wide open. Set MCP_AUTH_TOKEN
    # yourself for a stable value across restarts (e.g. so a browser tab
    # left open doesn't need re-pasting every time you restart the server).
    mcp_auth_token = os.environ.get("MCP_AUTH_TOKEN")
    if not mcp_auth_token:
        mcp_auth_token = secrets.token_urlsafe(24)
        print(
            "\n─── No MCP_AUTH_TOKEN set — generated one for this run "
            + "─" * 10
            + f"\n\n    {mcp_auth_token}\n\n"
            "Paste this into the chat UI's MCP connect panel to authenticate.\n"
        )

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
    http_app.add_middleware(BearerAuthMiddleware, token=mcp_auth_token)
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://127.0.0.1:{web_port}", f"http://localhost:{web_port}"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Accept", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )
    # Port overridable so tests/test_http_routes.py can boot a throwaway
    # instance without colliding with a real dev.server session already
    # running on the default port.
    uvicorn.run(http_app, host="127.0.0.1", port=int(os.environ.get("MCP_SERVER_PORT", "8787")))
