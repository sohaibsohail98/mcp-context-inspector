"""The OAuth 2.1 authorization server (RFC 9728/8414/7591 + PKCE) that
lets any spec-compliant MCP client (claude.ai Connectors, Cursor,
Copilot, ...) discover, register, and authenticate against this server
without a manually pasted token.

This server acts as BOTH the OAuth resource server (the /mcp endpoint,
gated by MultiTokenAuthMiddleware) and the authorization server — the
MCP spec allows either, and combined is simplest here since there's no
identity provider to delegate to beyond Google sign-in, which this
server already verifies itself.

Flow:
  1. Client hits /mcp with no/bad token -> 401 with a WWW-Authenticate
     header pointing at /.well-known/oauth-protected-resource/mcp (see
     MultiTokenAuthMiddleware).
  2. Client fetches that, learns this server is also its own
     authorization server, fetches /.well-known/oauth-authorization-server
     for the actual endpoints.
  3. Client POSTs /oauth/register once (RFC 7591) to get a client_id —
     no manual setup, no client secret (public client, PKCE-protected).
  4. Client opens /oauth/authorize in a browser; user signs in with
     Google (this server's existing identity check, reused as the
     consent step); server redirects back with a one-time code.
  5. Client exchanges the code at /oauth/token (with the PKCE verifier)
     for an access token — an ordinary bearer token, so
     MultiTokenAuthMiddleware needs no special-casing to accept it (see
     auth_store.is_valid_token, which checks both sign-in tokens and
     OAuth-issued ones).
"""

import json
import os
import time
from collections import defaultdict, deque
from html import escape
from urllib.parse import urlencode, urlparse

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from mcp_server.auth import store as auth_store
from mcp_server.app import current_owner, server
from mcp_server.auth.google import InvalidGoogleToken, verify_credential
from mcp_server.middleware import _public_origin
from mcp_server.routes.auth import _PAGE_STYLE

# Per-IP fixed-window rate limit for POST /oauth/register — that route is
# fully open by design (no auth, no CAPTCHA; RFC 7591 Dynamic Client
# Registration is meant to be self-service), and every call inserts a
# permanent row with no cleanup path except wiping the whole auth DB (see
# docs/nextsteps.md item 5a). In-memory, per-process: doesn't survive a
# restart or share state across multiple instances. Acceptable at this
# server's personal-project scale (min-instances=1 in prod today) the
# same way other in-memory tradeoffs already made in this codebase are;
# revisit with a real shared store (e.g. the same DynamoDB backend as
# auth_store) if this ever needs to hold under real concurrent abuse.
_REGISTER_WINDOW_SECONDS = 3600
_REGISTER_MAX_PER_WINDOW = 5
_register_attempts = defaultdict(deque)


def _client_ip(request):
    """The real visitor IP, not Cloudflare's own edge IP. The
    cloudflare-proxy Worker forwards the incoming request's headers
    unchanged (see cloudflare-proxy/worker.js) other than Host, so
    CF-Connecting-IP — set by Cloudflare's edge on every request it
    fronts — survives the hop to Cloud Run. request.client.host is only
    a reasonable fallback for local dev / direct-to-origin calls, where
    it IS the real caller."""
    return request.headers.get("CF-Connecting-IP") or (request.client.host if request.client else "unknown")


def _register_rate_limited(request):
    """True if this IP has already made _REGISTER_MAX_PER_WINDOW calls to
    /oauth/register within the trailing _REGISTER_WINDOW_SECONDS. Records
    the current attempt as a side effect when NOT rate-limited — an
    attempt that gets rejected doesn't itself count against the window."""
    now = time.monotonic()
    attempts = _register_attempts[_client_ip(request)]
    while attempts and now - attempts[0] > _REGISTER_WINDOW_SECONDS:
        attempts.popleft()
    if len(attempts) >= _REGISTER_MAX_PER_WINDOW:
        return True
    attempts.append(now)
    return False


def _oauth_cors_json(payload, status_code=200):
    """These endpoints are meant to be reachable cross-origin from
    whatever chat UI is doing the discovery/registration/token-exchange —
    that's the entire point of them existing — so they carry their own
    permissive CORS headers rather than relying on the app-wide
    CORSMiddleware, which is deliberately scoped to this deployment's own
    known origins (see CHAT_UI_ORIGIN) and would otherwise block this."""
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Cache-Control": "no-store",
        },
    )


def _mcp_resource_url(request):
    """The canonical resource URI (RFC 8707) this server's tokens are
    scoped to — always the /mcp endpoint itself, never a trailing slash
    (see the MCP spec's canonical-URI guidance). Built from
    _public_origin, not request.base_url directly — see that function's
    docstring for why."""
    return _public_origin(request) + "/mcp"


def _validate_oauth_request(client_id, redirect_uri, code_challenge, code_challenge_method, resource, request, state=None):
    """Shared validation for both the GET (initial request) and POST
    (post-Google-sign-in completion) halves of /oauth/authorize. Raises
    ValueError with a message safe to show the caller; returns
    (client_dict, resource_to_record).

    code_challenge_method: strictly `== "S256"`, not the previous
    `if code_challenge_method and ... != "S256"` — that guard was falsy
    for an empty string, so `code_challenge_method=` (present but blank)
    sailed straight through and only failed much later, confusingly, at
    redemption. There's no legitimate caller for whom "" or "plain"
    should be accepted; the GET route already defaults a fully-absent
    param to "S256" before this function ever sees it.

    state length cap: defense in depth alongside the escaping in
    oauth_authorize_page below — state is attacker-reachable (a phishing
    link can set it to anything) and free-form per spec, so there's no
    charset to validate, but an unbounded value has no legitimate use and
    a length cap costs nothing."""
    if code_challenge_method != "S256":
        raise ValueError("code_challenge_method must be S256")
    if not code_challenge:
        raise ValueError("code_challenge is required")
    if not redirect_uri:
        raise ValueError("redirect_uri is required")
    if state is not None and len(state) > 2048:
        raise ValueError("state is too long")
    client = auth_store.get_oauth_client(client_id) if client_id else None
    if client is None:
        raise ValueError("unknown client_id — register via /oauth/register first")
    if redirect_uri not in client["redirect_uris"]:
        raise ValueError("redirect_uri is not registered for this client")
    expected_resource = _mcp_resource_url(request)
    if resource and resource.rstrip("/") != expected_resource.rstrip("/"):
        raise ValueError("resource does not match this server")
    return client, (resource or expected_resource)


def _json_for_script(obj):
    """json.dumps(), but safe to interpolate directly into an HTML
    <script> block. Plain json.dumps() does NOT escape "<" or "/", so a
    value containing the literal substring "</script>" closes the
    enclosing script tag at the HTML-parser level — before the browser
    ever gets to JS string-escaping rules, which don't apply yet at that
    point. Escaping "<", ">", and "&" as \\u-sequences (valid inside any
    JSON/JS string, invisible to JSON.parse) neutralizes that regardless
    of where in the object the attacker-controlled value sits. This
    matters here specifically because /oauth/authorize embeds the
    caller-supplied `state` (and redirect_uri, code_challenge) query
    params, which are otherwise unvalidated free-form strings per the
    OAuth spec — an attacker who registers a client (open, unauthenticated
    registration) can put an XSS payload directly in `state` and send the
    resulting /oauth/authorize link to a victim who's already signed in,
    whose token then sits reachable in localStorage on this exact page."""
    return json.dumps(obj).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


@server.custom_route("/.well-known/oauth-protected-resource", methods=["GET", "OPTIONS"])
@server.custom_route("/.well-known/oauth-protected-resource/mcp", methods=["GET", "OPTIONS"])
async def oauth_protected_resource_metadata(request: Request):
    """RFC 9728 — tells a client where its authorization server is. Two
    paths registered: the bare well-known path some clients probe by
    default, and the resource-specific one (.../oauth-protected-resource/mcp)
    that RFC 9728 §3.1 actually specifies for a resource at /mcp."""
    if request.method == "OPTIONS":
        return _oauth_cors_json({})
    origin = _public_origin(request)
    return _oauth_cors_json({
        "resource": _mcp_resource_url(request),
        "authorization_servers": [origin],
        "bearer_methods_supported": ["header"],
    })


@server.custom_route("/.well-known/oauth-authorization-server", methods=["GET", "OPTIONS"])
async def oauth_authorization_server_metadata(request: Request):
    """RFC 8414 — the actual endpoint URLs and capabilities of this
    server acting as its own authorization server."""
    if request.method == "OPTIONS":
        return _oauth_cors_json({})
    origin = _public_origin(request)
    return _oauth_cors_json({
        "issuer": origin,
        "authorization_endpoint": origin + "/oauth/authorize",
        "token_endpoint": origin + "/oauth/token",
        "registration_endpoint": origin + "/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": ["mcp"],
    })


@server.custom_route("/oauth/register", methods=["POST", "OPTIONS"])
async def oauth_register(request: Request):
    """RFC 7591 Dynamic Client Registration — lets a client obtain a
    client_id with no manual setup. Public clients only (PKCE protects
    the actual grant instead of a client secret), matching
    token_endpoint_auth_methods_supported above."""
    if request.method == "OPTIONS":
        return _oauth_cors_json({})
    if _register_rate_limited(request):
        return _oauth_cors_json(
            {"error": "invalid_client_metadata", "error_description": "too many registration attempts, try again later"},
            429,
        )
    try:
        body = await request.json()
    except ValueError:
        return _oauth_cors_json({"error": "invalid_client_metadata", "error_description": "malformed JSON body"}, 400)
    if not isinstance(body, dict):
        return _oauth_cors_json({"error": "invalid_client_metadata"}, 400)

    try:
        client_id = auth_store.register_oauth_client(
            body.get("redirect_uris"),
            client_name=body.get("client_name"),
            token_endpoint_auth_method=body.get("token_endpoint_auth_method", "none"),
        )
    except ValueError as e:
        return _oauth_cors_json({"error": "invalid_client_metadata", "error_description": str(e)}, 400)

    client = auth_store.get_oauth_client(client_id)
    return _oauth_cors_json(
        {
            "client_id": client["client_id"],
            "client_name": client["client_name"],
            "redirect_uris": client["redirect_uris"],
            "token_endpoint_auth_method": client["token_endpoint_auth_method"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        },
        201,
    )


@server.custom_route("/oauth/authorize", methods=["GET"])
async def oauth_authorize_page(request: Request):
    """Renders a sign-in page for the OAuth authorization request — reuses
    this server's existing Google sign-in (see /auth/login) as the
    identity check AND the consent step: signing in here is what grants
    the requesting client access, same one-click model as everywhere else
    on this server. Validates the request up front so a malformed or
    unregistered client fails fast with a clear message instead of only
    after the user signs in."""
    q = request.query_params
    if q.get("response_type") != "code":
        return PlainTextResponse("response_type must be 'code'", status_code=400)
    try:
        client, resource = _validate_oauth_request(
            q.get("client_id"),
            q.get("redirect_uri"),
            q.get("code_challenge"),
            q.get("code_challenge_method", "S256"),
            q.get("resource"),
            request,
            state=q.get("state"),
        )
    except ValueError as e:
        return PlainTextResponse(str(e), status_code=400)

    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not google_client_id:
        return PlainTextResponse("Google sign-in isn't configured on this server", status_code=503)

    client_name = client["client_name"] or "This application"
    # Shown on the consent screen alongside client_name below: client_name
    # is free-text set at registration time and proves nothing (anyone can
    # register a client named "Claude") — the redirect host is the one
    # thing here that's actually bound to the registration and can't be
    # spoofed without also controlling that domain, so it's the signal
    # worth a user's attention, not the name.
    redirect_host = urlparse(q.get("redirect_uri")).netloc
    oauth_params_json = _json_for_script({
        "client_id": client["client_id"],
        "redirect_uri": q.get("redirect_uri"),
        "code_challenge": q.get("code_challenge"),
        "state": q.get("state"),
        "resource": resource,
    })
    return HTMLResponse(f"""<!doctype html>
<html><head><title>Authorize &middot; mcp-context-inspector</title>
<style>{_PAGE_STYLE}</style>
<script src="https://accounts.google.com/gsi/client" async defer></script>
</head><body>
<div class="narrow-page">
<div class="card accent">
  <h3>Authorize {escape(client_name)}</h3>
  <p class="card-hint">Sign in with Google to let <strong>{escape(client_name)}</strong>
  (<code>{escape(redirect_host)}</code>) connect to mcp-context-inspector as you. It gets the
  same access your own signed-in session already has — your own data only, scoped to your
  account. <strong>Check the domain in parentheses</strong> — a display name alone can say
  anything; that domain is where your data actually goes.</p>
  <div id="g_id_onload" data-client_id="{google_client_id}" data-callback="onOAuthSignIn"></div>
  <div class="g_id_signin" data-type="standard" data-theme="filled_black"></div>
  <div id="oauth-result" style="margin-top:0.7rem; font-size:0.85rem;"></div>
</div>
</div>
<script>
  const oauthParams = {oauth_params_json};
  async function onOAuthSignIn(response) {{
    const result = document.getElementById("oauth-result");
    result.textContent = "Authorizing…";
    try {{
      const res = await fetch("/oauth/authorize", {{
        method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify(Object.assign({{credential: response.credential}}, oauthParams)),
      }});
      const data = await res.json();
      if (res.ok && data.redirect) {{
        window.location.href = data.redirect;
      }} else {{
        result.innerHTML = '<span style="color:var(--err);">' + (data.error_description || data.error || "Something went wrong.") + '</span>';
      }}
    }} catch (err) {{
      result.innerHTML = '<span style="color:var(--err);">' + err.message + '</span>';
    }}
  }}
</script>
</body></html>""")


@server.custom_route("/oauth/authorize", methods=["POST"])
async def oauth_authorize_complete(request: Request):
    """Completes the authorize step after the browser has a fresh Google
    credential: re-validates the request (defense in depth — don't trust
    client-supplied params just because the GET page rendered), verifies
    the credential server-side the same way /auth/verify does, and mints
    a one-time authorization code the client exchanges at /oauth/token."""
    google_client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not google_client_id:
        return JSONResponse({"error": "server_error", "error_description": "GOOGLE_OAUTH_CLIENT_ID not configured"}, status_code=503)

    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    credential = body.get("credential")
    if not credential:
        return JSONResponse({"error": "invalid_request", "error_description": "missing credential"}, status_code=400)

    try:
        client, resource = _validate_oauth_request(
            body.get("client_id"),
            body.get("redirect_uri"),
            body.get("code_challenge"),
            "S256",
            body.get("resource"),
            request,
            state=body.get("state"),
        )
    except ValueError as e:
        return JSONResponse({"error": "invalid_request", "error_description": str(e)}, status_code=400)

    try:
        identity = verify_credential(credential, google_client_id)
    except InvalidGoogleToken as e:
        return JSONResponse({"error": "access_denied", "error_description": f"invalid Google credential: {e}"}, status_code=401)

    code = auth_store.issue_oauth_code(
        client["client_id"],
        identity["sub"],
        identity["email"],
        body.get("redirect_uri"),
        body.get("code_challenge"),
        resource,
    )
    params = {"code": code}
    if body.get("state"):
        params["state"] = body["state"]
    redirect_uri = body.get("redirect_uri")
    separator = "&" if "?" in redirect_uri else "?"
    return JSONResponse({"redirect": redirect_uri + separator + urlencode(params)})


@server.custom_route("/oauth/token", methods=["POST", "OPTIONS"])
async def oauth_token(request: Request):
    """Exchanges a one-time authorization code (+ PKCE verifier) for an
    access token — an ordinary bearer token, same format and same
    MultiTokenAuthMiddleware check as every other token this server
    issues (see auth_store.mint_oauth_token)."""
    if request.method == "OPTIONS":
        return _oauth_cors_json({})

    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return _oauth_cors_json({"error": "unsupported_grant_type"}, 400)

    code = form.get("code")
    client_id = form.get("client_id")
    redirect_uri = form.get("redirect_uri")
    code_verifier = form.get("code_verifier")
    if not all([code, client_id, redirect_uri, code_verifier]):
        return _oauth_cors_json({"error": "invalid_request", "error_description": "missing required parameter"}, 400)

    resource = form.get("resource") or _mcp_resource_url(request)
    try:
        google_sub, email = auth_store.redeem_oauth_code(code, client_id, redirect_uri, code_verifier, resource)
    except ValueError as e:
        return _oauth_cors_json({"error": "invalid_grant", "error_description": str(e)}, 400)

    client = auth_store.get_oauth_client(client_id)
    client_name = (client or {}).get("client_name") or "OAuth client"
    token = auth_store.mint_oauth_token(google_sub, email, client_name)
    return _oauth_cors_json({
        "access_token": token,
        "token_type": "Bearer",
        # This server's bearer tokens don't actually expire (see
        # auth_store) — a large-but-finite value here rather than
        # omitting the field, since some clients treat a missing
        # expires_in as "unknown, refresh soon" rather than "doesn't
        # expire."
        "expires_in": 60 * 60 * 24 * 365,
        "scope": "mcp",
    })


def _require_owner(request):
    """True if the caller connected with the owner token, not a per-user
    one (current_owner.get() is None — see app.py's contextvar docstring
    and MultiTokenAuthMiddleware). The admin routes below list/revoke
    every OAuth client and token on the server, not just the caller's
    own — owner-only, same "it's your server" boundary already used
    elsewhere (e.g. get_recent_sessions seeing everyone's data)."""
    return current_owner.get() is None


@server.custom_route("/api/oauth-clients", methods=["GET"])
async def api_list_oauth_clients(request: Request):
    if not _require_owner(request):
        return JSONResponse({"error": "owner token required"}, status_code=403)
    return JSONResponse(auth_store.list_oauth_clients())


@server.custom_route("/api/oauth-clients/{client_id}", methods=["DELETE"])
async def api_revoke_oauth_client(request: Request):
    if not _require_owner(request):
        return JSONResponse({"error": "owner token required"}, status_code=403)
    auth_store.revoke_oauth_client(request.path_params["client_id"])
    return JSONResponse({"ok": True})


@server.custom_route("/api/oauth-tokens", methods=["GET"])
async def api_list_oauth_tokens(request: Request):
    if not _require_owner(request):
        return JSONResponse({"error": "owner token required"}, status_code=403)
    return JSONResponse(auth_store.list_oauth_tokens())


@server.custom_route("/api/oauth-tokens/{token_prefix}", methods=["DELETE"])
async def api_revoke_oauth_token(request: Request):
    """Revokes by exact token, not prefix — list_oauth_tokens() only ever
    shows a prefix (never the full value), so this expects the caller to
    already have the real token from wherever it was issued (e.g. their
    own client config), matching how every other revoke in this server
    works. The path param is named token_prefix only because that's what
    list_oauth_tokens() shows an admin to identify which row to revoke;
    pass the full token here."""
    if not _require_owner(request):
        return JSONResponse({"error": "owner token required"}, status_code=403)
    auth_store.revoke_oauth_token(request.path_params["token_prefix"])
    return JSONResponse({"ok": True})


