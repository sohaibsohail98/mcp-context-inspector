"""Regression tests for two production bugs found via live testing against
the deployed instance (behind the Cloudflare Worker reverse proxy; see
cloudflare-proxy/worker.js) that no earlier test caught, because none of
them build the app the way `__main__` actually does:

1. The app-wide CORSMiddleware enforces its CHAT_UI_ORIGIN allowlist
   against EVERY OPTIONS preflight regardless of path, so a real OAuth
   client's preflight to /oauth/register from an origin like
   https://claude.ai got hard-rejected before ever reaching that route's
   own permissive CORS handling. Fixed by OAuthCORSMiddleware, added as
   the outermost middleware so it intercepts those preflights first.

2. request.base_url reflects Cloud Run's internal http://*.run.app
   origin (TLS terminated before the container, Host header rewritten by
   the proxy), not the public https:// URL a client actually used, so
   every OAuth metadata/URL built from it was simply wrong once deployed,
   even though every in-process TestClient test (base_url already
   correct there) passed. Fixed by _public_origin, honoring PUBLIC_ORIGIN.

Same lesson as test_transport_security.py's 421 bug: build the app the
way production actually does (explicit middleware stack, not the bare
`streamable_http_app()` other test files use), or a production-shaped bug
stays invisible.
"""

import pytest
from starlette.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient

from mcp_server import server as server_module


def _build_prod_style_app():
    """Mirrors mcp_server/server.py's `__main__` block: auth, then the
    app-wide restrictive CORSMiddleware, then OAuthCORSMiddleware added
    last (so it's outermost), the exact order production uses."""
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8788", "http://localhost:8788"],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Mcp-Session-Id", "Accept", "Authorization"],
        expose_headers=["Mcp-Session-Id"],
    )
    app.add_middleware(server_module.OAuthCORSMiddleware)
    return app


@pytest.fixture
def prod_client(isolated_auth_store, isolated_sqlite_db):
    with TestClient(_build_prod_style_app()) as c:
        yield c


# --- OAuth preflight must bypass the app-wide CORS allowlist -------------


def test_oauth_register_preflight_succeeds_from_an_unlisted_origin(prod_client):
    """https://claude.ai is NOT in CHAT_UI_ORIGIN's allowlist, so this must
    still succeed, since /oauth/* has to be reachable from any client."""
    resp = prod_client.options(
        "/oauth/register",
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_oauth_token_preflight_succeeds_from_an_unlisted_origin(prod_client):
    resp = prod_client.options(
        "/oauth/token",
        headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_well_known_oauth_preflight_succeeds_from_an_unlisted_origin(prod_client):
    resp = prod_client.options(
        "/.well-known/oauth-authorization-server",
        headers={"Origin": "https://claude.ai", "Access-Control-Request-Method": "GET"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "*"


def test_unrelated_protected_route_preflight_is_still_restricted(prod_client):
    """The fix must be scoped to /oauth/ and /.well-known/oauth-* only:
    /api/* (same sensitive session data as the MCP tools) must keep its
    existing CHAT_UI_ORIGIN-only restriction, or this "fix" would have
    quietly reopened a route that was deliberately locked down."""
    resp = prod_client.options(
        "/api/sessions",
        headers={
            "Origin": "https://claude.ai",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 400


# --- _public_origin --------------------------------------------------------


def test_public_origin_env_var_overrides_request_base_url(prod_client, monkeypatch):
    """Simulates the actual production bug: request.base_url inside the
    TestClient is http://testserver (analogous to Cloud Run's internal
    origin). PUBLIC_ORIGIN must win over it for every URL this server
    generates about itself."""
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://ctxwindow.example.com")

    resp = prod_client.get("/.well-known/oauth-authorization-server")
    body = resp.json()
    assert body["issuer"] == "https://ctxwindow.example.com"
    assert body["token_endpoint"] == "https://ctxwindow.example.com/oauth/token"

    resp = prod_client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.json()["resource"] == "https://ctxwindow.example.com/mcp"


def test_public_origin_unset_falls_back_to_request_base_url(prod_client, monkeypatch):
    monkeypatch.delenv("PUBLIC_ORIGIN", raising=False)
    resp = prod_client.get("/.well-known/oauth-authorization-server")
    assert resp.json()["issuer"] == "http://testserver"


def test_401_www_authenticate_honors_public_origin(prod_client, monkeypatch):
    monkeypatch.setenv("PUBLIC_ORIGIN", "https://ctxwindow.example.com")
    resp = prod_client.post("/mcp", json={})
    assert resp.status_code == 401
    assert "https://ctxwindow.example.com/.well-known/oauth-protected-resource/mcp" in resp.headers["www-authenticate"]
