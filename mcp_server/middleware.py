"""Auth/CORS middleware and the request-scoped public-origin helper.
Used by mcp_server/routes/auth.py, routes/oauth.py, and routes/setup.py
(all need `_public_origin`), and by server.py to wire these middleware
classes onto the Starlette app."""

import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from mcp_server.app import current_owner
from mcp_server.auth import store as auth_store

# Demo-capture bypass (see scripts/demo_capture.py and
# mcp_server/routes/demo.py). A fixed, obviously-fake token rather than
# something minted per run, since the whole point is a reproducible
# recording against the seeded demo/metrics.db: a random token would
# make the capture script's own auth setup non-deterministic for no
# benefit. Only ever accepted when CTXWINDOW_DEMO_MODE=1, which is unset
# in every real deployment, so this constant being public in source
# control grants no access anywhere it matters.
DEMO_TOKEN = "ctxwindow-demo-token-do-not-use-in-production"


def _demo_mode_enabled():
    return os.environ.get("CTXWINDOW_DEMO_MODE") == "1"


def _public_origin(request):
    """The real, public-facing origin this server is reachable at. NOT
    `request.base_url`, which reflects whatever THIS process actually
    sees. Behind the Cloudflare Worker reverse proxy (see
    cloudflare-proxy/worker.js), that's Cloud Run's own internal
    http://<hash>.run.app origin: Cloud Run terminates TLS before the
    container sees the request (so the observed scheme is always "http",
    even for a real https:// caller), and the worker deletes the inbound
    Host header and lets fetch() re-derive it from env.ORIGIN, so the
    Host this process sees is Cloud Run's own raw hostname, never the
    public one a client actually talked to. Used for URLs this server
    generates ABOUT itself, such as OAuth issuer/resource metadata and
    the downloadable local-setup script. Getting this wrong doesn't 401
    or crash anything: a wrong-but-well-formed URL only fails later, when
    something else tries to fetch it.

    Falls back to request.base_url when PUBLIC_ORIGIN isn't set, which
    is correct for local dev (no proxy in between) and for the
    loopback-only /setup/apply-local-config route."""
    configured = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")
    return configured or str(request.base_url).rstrip("/")


class MultiTokenAuthMiddleware(BaseHTTPMiddleware):
    """Gates /mcp and /api/ behind a bearer token that's either the
    owner's single shared token (`owner_token`, printed to stdout on
    startup) or a per-user token minted via /auth/login and /auth/verify
    (`auth_store.is_valid_token`). /api/ is protected too, since those
    routes return the same session data as the MCP tools; leaving them
    open would make the MCP-side auth pointless. /auth/* itself stays
    unauthenticated, since that is the pre-auth sign-in flow.

    GET /setup/install is also carved out of the bearer-token gate
    (see unauthenticated_exact_paths). It's the curl-able one-liner's
    target (`curl ... | sh`), which can't carry an Authorization header,
    so its credential is the short-lived `?t=` code checked inside the
    route handler itself (routes/setup.py's setup_install), not a bearer
    token here.

    Also sets `current_owner` for the rest of this request, so every
    read/write below can filter to the caller's own data. See
    metrics/store.py's owner param and the README's Auth section."""

    def __init__(
        self,
        app,
        owner_token,
        protected_prefixes=("/mcp", "/api/", "/otlp", "/setup"),
        unauthenticated_exact_paths=("/setup/install",),
    ):
        super().__init__(app)
        self.owner_token = owner_token
        self.protected_prefixes = protected_prefixes
        self.unauthenticated_exact_paths = unauthenticated_exact_paths

    async def dispatch(self, request, call_next):
        if request.url.path in self.unauthenticated_exact_paths:
            return await call_next(request)
        if any(request.url.path.startswith(p) for p in self.protected_prefixes):
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else None
            if token == self.owner_token:
                current_owner.set(None)
            elif token == DEMO_TOKEN and _demo_mode_enabled():
                # Same visibility as the owner token (current_owner=None),
                # which is what the seeded demo rows need: seed_demo_db.py
                # inserts them with owner=NULL, so a per-user token would
                # see nothing (see metrics/store_sqlite.py's _visible()).
                current_owner.set(None)
            elif token and auth_store.is_valid_token(token):
                current_owner.set(auth_store.get_sub_for_token(token))
            else:
                # WWW-Authenticate here is what lets an MCP client do OAuth
                # discovery (RFC 9728 §5.1): on a 401 with no/invalid token,
                # it looks at this header to find the protected-resource
                # metadata document, which in turn points it at
                # /.well-known/oauth-authorization-server (see the OAuth
                # routes below). Also set on this specific response, rather
                # than relying on the global CORSMiddleware, since a
                # browser-side client's discovery fetch needs the header
                # readable cross-origin even though this is a plain 401.
                metadata_url = _public_origin(request) + "/.well-known/oauth-protected-resource/mcp"
                return JSONResponse(
                    {"error": "unauthorized: missing or wrong bearer token"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": f'Bearer error="invalid_token", resource_metadata="{metadata_url}"',
                        "Access-Control-Allow-Origin": "*",
                    },
                )
        return await call_next(request)


class OAuthCORSMiddleware(BaseHTTPMiddleware):
    """Short-circuits CORS preflight (OPTIONS) requests to the OAuth
    discovery/registration/token routes before the app-wide CORSMiddleware
    ever sees them.

    CORSMiddleware enforces its allow_origins allowlist against EVERY
    OPTIONS request regardless of path; there's no per-route exclusion.
    That allowlist is CHAT_UI_ORIGIN (this deployment's own known chat-UI
    origins), which deliberately does NOT include arbitrary MCP clients
    like claude.ai, Cursor, or a Copilot backend. Those routes need to
    be reachable from literally any origin, since discovery/registration
    is the entire point of them being public. Left to the app-wide
    middleware, a preflight from an unrecognized origin gets hard-
    rejected with a generic 400 before ever reaching the OAuth route's
    own permissive CORS handling, which is exactly what broke
    registration for a real client in production before this existed.

    Only handles the preflight (OPTIONS) case; the routes themselves
    already set Access-Control-Allow-Origin: * on their actual GET/POST
    responses."""

    OAUTH_PREFIXES = ("/oauth/", "/.well-known/oauth-")

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS" and any(request.url.path.startswith(p) for p in self.OAUTH_PREFIXES):
            return JSONResponse(
                {},
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Access-Control-Allow-Headers": "Authorization, Content-Type",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                },
            )
        return await call_next(request)
