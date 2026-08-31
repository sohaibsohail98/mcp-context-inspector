"""MCP server for agent execution metrics. Exposes 7 read-only tools plus
one write tool (record_session, for a caller's own remote agent to push
its own data in) over Streamable HTTP, plus a few plain REST routes as a
curl-friendly alternative to a real MCP handshake. Both paths call the
same underlying functions in metrics/store.py, so there is one
data-access layer rather than two implementations of "how do I read a
session."

A thin entrypoint: shared state lives in app.py, auth/CORS middleware in
middleware.py, MCP tools in tools.py, and REST/OTLP/OAuth/setup routes
under routes/. Importing those modules below is what registers their
tools/routes onto the shared `server` instance (decorator side effects);
this file just imports them, then wires up uvicorn.

Run from repo root:
    uv run python -m mcp_server.server
"""

import os

from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware

# Importing the `tools` and `routes.*` modules below registers their
# @server.tool()/@server.custom_route() handlers. Side-effect imports;
# order doesn't matter between them.
from mcp_server import tools  # noqa: F401,E402
from mcp_server.app import server  # noqa: F401 (re-exported for tests/other modules)
from mcp_server.middleware import MultiTokenAuthMiddleware, OAuthCORSMiddleware
from mcp_server.routes import api as routes_api  # noqa: F401,E402
from mcp_server.routes import auth as routes_auth  # noqa: F401,E402
from mcp_server.routes import demo as routes_demo  # noqa: F401,E402
from mcp_server.routes import docs as routes_docs  # noqa: F401,E402
from mcp_server.routes import oauth as routes_oauth  # noqa: F401,E402
from mcp_server.routes import otlp as routes_otlp  # noqa: F401,E402
from mcp_server.routes import setup as routes_setup  # noqa: F401,E402
from mcp_server.routes import webapp as routes_webapp  # noqa: F401,E402
from mcp_server.routes import wellknown as routes_wellknown  # noqa: F401,E402


def _maybe_seed_demo_db(demo_seed_src, target_path):
    """Public demo deployment: the image bakes a read-only demo dataset
    (DEMO_SEED_SRC, e.g. demo/metrics.db, built by scripts/seed_demo_db.py)
    but METRICS_DB_PATH points writes at an ephemeral path (e.g. /tmp) so
    a signed-in visitor's own record_session calls don't touch the image
    layer. Copies the seed in on first boot only, so a cold start resets
    it. That is the documented tradeoff, not a bug. No-op if
    demo_seed_src isn't set or target_path already exists."""
    if not demo_seed_src or target_path.exists():
        return False
    target_path.parent.mkdir(parents=True, exist_ok=True)
    import shutil

    shutil.copyfile(demo_seed_src, target_path)
    return True


def _resolve_owner_token(env_value):
    """.strip(): Secret Manager values are frequently created via
    `echo "token" | gcloud secrets create ...`, which appends a trailing
    newline that becomes part of the mounted env var. An untrimmed
    comparison in MultiTokenAuthMiddleware would then reject every real
    request carrying the actual token. Returns None for unset/blank,
    same as no MCP_AUTH_TOKEN configured."""
    return (env_value or "").strip() or None


if __name__ == "__main__":
    import secrets

    import uvicorn

    from metrics.store_sqlite import DB_PATH as _metrics_db_path

    _maybe_seed_demo_db(os.environ.get("DEMO_SEED_SRC"), _metrics_db_path)

    # No token configured: generate one for this run and print it, rather
    # than starting the /mcp endpoint wide open. Set MCP_AUTH_TOKEN
    # yourself for a stable value across restarts (e.g. so a browser tab
    # left open doesn't need re-pasting every time you restart the server).
    # This is the OWNER's token. Friends should sign in via /auth/login
    # instead of being handed this one, so revoking a single friend's
    # access doesn't mean rotating everyone's token.
    mcp_auth_token = _resolve_owner_token(os.environ.get("MCP_AUTH_TOKEN"))
    if not mcp_auth_token:
        mcp_auth_token = secrets.token_urlsafe(24)
        print(
            "\n─── No MCP_AUTH_TOKEN set, generated one for this run " + "─" * 10 + f"\n\n    {mcp_auth_token}\n\n"
            "This is YOUR (owner) token. Paste it into your MCP-config-based "
            "client (Claude Code, curl) to authenticate. The chat "
            "UI's own MCP panel uses Google sign-in instead, not this token.\n",
            flush=True,
        )

    server_port = int(os.environ.get("MCP_SERVER_PORT", "8787"))
    if os.environ.get("GOOGLE_OAUTH_CLIENT_ID"):
        # Google Identity Services only reliably honors "localhost" as an
        # authorized origin for plain-HTTP local development. 127.0.0.1,
        # though it resolves to the same loopback server, is a different
        # string, and GSI's runtime origin check can reject it live even
        # when the Cloud Console UI accepts saving it as an authorized
        # JavaScript origin. Console setup (README) uses localhost too.
        print(f"Google sign-in enabled. Friends can get their own token at http://localhost:{server_port}/auth/login\n")
    else:
        print(
            "GOOGLE_OAUTH_CLIENT_ID not set: /auth/login will report sign-in as unavailable; only the owner token above works.\n"
        )

    # server.run(transport="streamable-http") doesn't expose the
    # underlying Starlette app for CORS configuration, and a browser-side
    # MCP handshake running from another origin/port needs cross-origin
    # access to /mcp. Constructing the app via streamable_http_app()
    # ourselves and wrapping it in CORSMiddleware is the same server,
    # just runnable with the middleware attached.
    # Local dev origins for a browser-side client served from the
    # companion dev port; kept allowed alongside CHAT_UI_ORIGIN below.
    allowed_origins = ["http://127.0.0.1:8788", "http://localhost:8788"]
    # Comma-separated list of the deployed chat UI's real origin(s), e.g.
    # both a Cloud Run URL and a Cloudflare Worker reverse-proxy in front
    # of it (see cloudflare-proxy/), since a browser sees these as
    # different origins even though they ultimately reach the same
    # service. Local dev origins above stay allowed regardless.
    deployed_chat_origins = os.environ.get("CHAT_UI_ORIGIN", "")
    allowed_origins.extend(origin.strip() for origin in deployed_chat_origins.split(",") if origin.strip())
    # streamable_http_app()'s own `host` parameter defaults to "127.0.0.1"
    # (unused here, since we never pass it), and the SDK's lowlevel Server
    # silently *auto-enables* DNS-rebinding Host-header protection
    # scoped to that default whenever no explicit transport_security is
    # given (see mcp/server/lowlevel/server.py). That's fine for local
    # dev but on Cloud Run the real Host header the container sees is the
    # service's own `*.run.app` hostname: the Cloudflare Worker in front
    # (cloudflare-proxy/worker.js) deletes the inbound Host header and
    # lets fetch() re-derive it from env.ORIGIN, so it's never the
    # workers.dev proxy hostname either. Without an explicit allowlist
    # covering that real Cloud Run host, every production request gets
    # hard-rejected with 421 "Invalid Host header". This bit us live,
    # with Google sign-in on the chat UI failing with HTTP 421. Set
    # explicitly rather than skipped, so DNS-rebinding protection stays
    # *on* everywhere, just correctly scoped for this deployment.
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    # Comma-separated list of additional Host header values this
    # deployment's requests actually arrive with, e.g. the Cloud Run
    # service's own `<service>-<hash>-<region>.a.run.app` hostname (the
    # literal Host header value after the Cloudflare Worker proxy
    # rewrites it), same CHAT_UI_ORIGIN-style convention as above.
    deployed_hosts = os.environ.get("MCP_ALLOWED_HOSTS", "")
    allowed_hosts.extend(host.strip() for host in deployed_hosts.split(",") if host.strip())
    # Escape hatch for throwaway build/test sandboxes (e.g. a registry's
    # MCP-catalogue build pipeline) that reach the container over an
    # ephemeral internal Docker-network address -- a Host header like
    # "10.90.0.1:33579" that can't be allowlisted ahead of time because
    # the IP/port vary per run, and DNS-rebinding protection then 421s
    # POST /mcp while GET /health still passes. Off by default; NO real
    # deployment sets this (Cloud Run uses MCP_ALLOWED_HOSTS instead).
    disable_rebind = os.environ.get("MCP_DISABLE_DNS_REBINDING_PROTECTION", "").strip().lower() in ("1", "true", "yes")
    http_app = server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=not disable_rebind,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )
    )
    # Auth middleware added before CORS so CORS ends up outermost (Starlette
    # runs the most-recently-added middleware first). CORS preflight
    # (OPTIONS, no Authorization header) then gets handled and answered
    # before ever reaching the auth check, and CORS headers still land on 401s.
    http_app.add_middleware(MultiTokenAuthMiddleware, owner_token=mcp_auth_token)
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Accept", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )
    # Added last so it ends up the OUTERMOST middleware (runs before
    # CORSMiddleware). See OAuthCORSMiddleware's docstring for why an
    # OAuth client's preflight needs to bypass the app-wide CORS
    # allowlist entirely rather than going through it.
    http_app.add_middleware(OAuthCORSMiddleware)
    # HOST defaults to loopback-only for local dev; Cloud Run sets PORT
    # itself and requires binding 0.0.0.0, so HOST=0.0.0.0 is set in the
    # container's env there. Port overridable so tests can boot a
    # throwaway instance without colliding with a real dev.server session
    # already running on the default port.
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", server_port))
    uvicorn.run(http_app, host=host, port=port)
