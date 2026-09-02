"""Google sign-in, the connect page, and the live dashboard SPA: the
human-facing side of this server. auth_login serves the sign-in/connect/dashboard SPA from real files
under dashboard/ (index.html, dashboard.css, dashboard.js), the same
"static file, not a Python string template" way routes/webapp.py and
routes/docs.py serve their frontends, injecting only a single
window.__CFG__ <script> for the one server-side value the page needs.
auth_verify completes Google sign-in and mints this user's MCP token."""

import json
import os
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from mcp_server.app import server
from mcp_server.auth import store as auth_store
from mcp_server.auth import token_cache
from mcp_server.auth.google import InvalidGoogleToken, verify_credential
from mcp_server.middleware import _public_origin

# /auth/* is the pre-auth flow that mints a per-user MCP token, so it is
# deliberately NOT in MultiTokenAuthMiddleware's protected_prefixes.

# Persistent browser-session cookie. Its value IS the caller's per-device
# MCP token (already one row per browser via User-Agent in device_tokens,
# and non-expiring server-side), so /auth/session can hand it straight
# back with no Google re-prompt when localStorage was cleared. httpOnly
# so page JS can't read it (the token is still shown in the UI and used
# for fetches from the localStorage copy; the cookie is purely the
# recovery path). SameSite=Lax so a top-level nav back from an external
# link still carries it. Secure because ctxwindow.uk is always https at
# the edge; on plain-http localhost dev Starlette drops the Secure flag
# itself when the request scheme is http, so sign-in still works there.
_SESSION_COOKIE = "mci_session"
_SESSION_MAX_AGE = 60 * 60 * 24 * 90  # 90 days


def _set_session_cookie(response, token, secure):
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response):
    response.delete_cookie(_SESSION_COOKIE, path="/")


# The sign-in / connect / live-dashboard SPA used to be authored right
# here as ~2,000 lines of HTML+CSS+JS inside this handler's f-strings,
# with every JS/CSS `{` and `}` doubled to survive f-string parsing.
# It now lives as real files under dashboard/ (index.html, dashboard.css,
# dashboard.js, unavailable.html), served the same "static file, not a
# Python string template" way routes/webapp.py and routes/docs.py serve
# their frontends. auth_login still does the two genuinely dynamic bits:
# pick the 200 vs 503 page based on whether Google sign-in is configured,
# and inject a single <script>window.__CFG__ = {...}</script> block plus
# the Google Sign-In client script (and, in demo mode, the demo scripts)
# into index.html's <head>. The page JS reads the server-injected
# canonical origin from window.__CFG__ instead of an f-string splice.

_DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"

# Allowlist of servable static filenames -> media type, same shape and
# same traversal-safety reasoning as routes/webapp.py's _STATIC_FILES
# (an allowlist, never a path join against the request).
_DASHBOARD_STATIC = {
    "dashboard.css": "text/css",
    "dashboard.js": "text/javascript",
}

_GSI_SCRIPT_TAG = '<script src="https://accounts.google.com/gsi/client" async defer></script>'

# The dashboard.css text, for the one other page that still inlines this
# stylesheet in a <style> block rather than linking it: routes/oauth.py's
# OAuth consent screen (a small, separate page that shares the visual
# language but not the SPA). It imports _PAGE_STYLE from here, so the CSS
# stays single-sourced from the file even though that page hasn't been
# moved to a linked stylesheet. auth_login's own page links
# /auth/static/dashboard.css directly and does not use this.
#
# Read defensively: this runs at import time, and server.py imports this
# module on startup, so a bare read_text() turns a missing/unreadable
# dashboard.css into a server-wide boot failure (a broken deploy) rather
# than one slightly-unstyled consent screen. A missing file here means
# the Docker image is mis-built -- surface that as an unstyled page, not
# a crash loop.
try:
    _PAGE_STYLE = (_DASHBOARD_DIR / "dashboard.css").read_text()
except OSError:  # pragma: no cover - only hit on a mis-built image
    _PAGE_STYLE = ""


@server.custom_route("/auth/login", methods=["GET"])
async def auth_login(request: Request):
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        # Same 503 "sign-in not configured" page as before, now a static
        # file. It carries no Google button and no dashboard JS, so no
        # config injection is needed.
        return FileResponse(
            _DASHBOARD_DIR / "unavailable.html",
            media_type="text/html",
            status_code=503,
        )

    # Demo-only staged-reveal script (see demo_static/demo_reveal.js and
    # scripts/demo_capture.py). Double-gated: the env var alone isn't
    # enough, ?demo=1 must also be on the request, so a stray real visit
    # to a demo deployment doesn't get the staged-reveal treatment.
    demo_script_tag = ""
    if os.environ.get("CTXWINDOW_DEMO_MODE") == "1" and request.query_params.get("demo") == "1":
        demo_script_tag = (
            '<script src="/demo-static/demo_transition.js"></script>'
            '<script src="/demo-static/demo_reveal.js"></script>'
        )

    # The one server-injected value the page JS still needs: the canonical
    # public origin (PUBLIC_ORIGIN env, e.g. https://ctxwindow.uk), so
    # every URL we hand the user to paste elsewhere -- the MCP connector
    # URL, the config snippet, the OTLP endpoint, the curl/install
    # commands -- reads the same no matter which host they loaded this
    # page from. Same-origin fetch()es keep using relative paths.
    cfg = {"canonicalOrigin": _public_origin(request)}
    # Escape any "</" so the JSON can't break out of the <script> element
    # (a URL is very unlikely to contain one, but this is the standard
    # guard for JSON embedded in inline script).
    cfg_json = json.dumps(cfg).replace("</", "<\\/")
    head_inject = (
        f"<script>window.__CFG__ = {cfg_json};</script>\n"
        f"{_GSI_SCRIPT_TAG}"
    )

    html = (_DASHBOARD_DIR / "index.html").read_text()
    html = html.replace("<!--SERVER-HEAD-INJECT-->", head_inject)
    html = html.replace("<!--SERVER-BODY-INJECT-->", demo_script_tag)
    html = html.replace("__CLIENT_ID__", client_id)
    return HTMLResponse(html)


@server.custom_route("/auth/static/{filename}", methods=["GET"])
async def auth_static(request: Request):
    """The extracted dashboard.css / dashboard.js, served the same way
    routes/webapp.py serves /m/{filename}: an allowlist lookup, a
    FileResponse with an explicit media type, no path join against the
    request. Not gated by MultiTokenAuthMiddleware (no per-user data;
    /auth/* is outside its protected prefixes)."""
    filename = request.path_params["filename"]
    media_type = _DASHBOARD_STATIC.get(filename)
    if media_type is None:
        return RedirectResponse(url="/auth/login")
    return FileResponse(_DASHBOARD_DIR / filename, media_type=media_type)


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

    # Keep the shared account row (mcp_users) so admin list_users() still
    # shows this account and any pre-existing pasted token keeps working,
    # but hand the caller a PER-DEVICE token: one row per browser/machine
    # in device_tokens, so they can later revoke just one device from the
    # dashboard's "Devices" list without signing out everywhere. Repeat
    # sign-ins from the same User-Agent return the same device token, so
    # a config already pasted elsewhere isn't invalidated by a reload.
    auth_store.get_or_create_token(identity["sub"], identity["email"])
    user_agent = request.headers.get("user-agent", "")
    token = auth_store.get_or_create_device_token(identity["sub"], identity["email"], user_agent)
    resp = JSONResponse({"mcp_token": token, "email": identity["email"]})
    _set_session_cookie(resp, token, secure=request.url.scheme == "https")
    return resp


@server.custom_route("/auth/session", methods=["GET"])
async def auth_session(request: Request):
    """Recovery path for a returning visitor whose localStorage was
    cleared but who still has a valid mci_session cookie: hands the token
    and email back so the dashboard rehydrates without a fresh Google
    sign-in. Returns 401 (no body worth leaking) when there is no cookie,
    the cookie's token was revoked, or it has no account identity."""
    token = request.cookies.get(_SESSION_COOKIE)
    if not token or not auth_store.is_valid_token(token):
        resp = JSONResponse({"error": "no session"}, status_code=401)
        if token:
            _clear_session_cookie(resp)  # stale cookie, drop it
        return resp
    google_sub = auth_store.get_sub_for_token(token)
    email = None
    if google_sub:
        for u in auth_store.list_users():
            if u.get("google_sub") == google_sub:
                email = u.get("email")
                break
    if not email:
        return JSONResponse({"error": "no session"}, status_code=401)
    resp = JSONResponse({"mcp_token": token, "email": email})
    # Sliding refresh: bump the 90-day window on every successful use so
    # an active visitor's cookie never lapses.
    _set_session_cookie(resp, token, secure=request.url.scheme == "https")
    return resp


@server.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request: Request):
    """Clears the browser session cookie. The MCP token itself is not
    revoked here (a config pasted into a client elsewhere keeps working);
    use the dashboard's Devices list or the account-wide revoke for
    that."""
    resp = JSONResponse({"ok": True})
    _clear_session_cookie(resp)
    return resp


def _caller_token(request):
    """The bearer token on this request, or None. /auth/* is outside
    MultiTokenAuthMiddleware's protected prefixes (it's the pre-auth
    flow), so the two device-management routes below authenticate
    themselves against auth_store here."""
    header = request.headers.get("authorization", "")
    return header.removeprefix("Bearer ") if header.startswith("Bearer ") else None


@server.custom_route("/auth/devices", methods=["GET"])
async def auth_devices(request: Request):
    """The signed-in caller's own active devices/sessions: every
    per-device sign-in token plus every connector session, as one list.
    Requires a valid bearer token and only ever returns rows for the
    account that token belongs to (list_tokens is scoped by google_sub),
    so it can't be used to enumerate another user's devices. Never
    returns a raw token, only the token_id revoke handle."""
    token = _caller_token(request)
    if not token or not auth_store.is_valid_token(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    google_sub = auth_store.get_sub_for_token(token)
    if not google_sub:
        # A valid token with no google_sub is the shared owner token;
        # it has no per-device identity and no device list to show.
        return JSONResponse({"devices": []})
    devices = auth_store.list_tokens(google_sub, current_token=token)
    return JSONResponse({"devices": devices})


@server.custom_route("/auth/revoke-device", methods=["POST"])
async def auth_revoke_device(request: Request):
    """Revoke exactly one of the caller's own devices/sessions by its
    token_id. Ownership is enforced two ways: the caller must present a
    valid bearer token, and revoke_token is scoped to that token's
    google_sub, so passing another user's token_id simply matches
    nothing. Idempotent: revoking an already-gone token_id is still a
    200. Revoking the token_id the caller is currently holding is
    allowed (it's their device) and effectively signs this browser
    out."""
    token = _caller_token(request)
    if not token or not auth_store.is_valid_token(token):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    google_sub = auth_store.get_sub_for_token(token)
    if not google_sub:
        return JSONResponse({"error": "this token has no revocable devices"}, status_code=400)
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict) or not body.get("token_id"):
        return JSONResponse({"error": "missing token_id"}, status_code=400)
    auth_store.revoke_token(google_sub, body["token_id"])
    # No raw token here (revoke_token takes a token_id hash prefix), so
    # clear the whole cache -- it's small and short-lived -- to make the
    # revoke take effect this request rather than after the TTL.
    token_cache.clear()
    return JSONResponse({"ok": True})
