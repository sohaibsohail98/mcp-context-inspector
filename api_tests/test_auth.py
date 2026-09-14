"""Auth surface: /auth/login reachability, and that every protected
prefix (mcp_server/middleware.py's MultiTokenAuthMiddleware) actually
rejects a missing/invalid bearer token. Does NOT exercise /auth/verify's
real Google-credential exchange, since that needs a live Google OAuth
round-trip, not something a black-box HTTP test can drive without a
real browser/user. See tests/test_oauth.py for the in-process,
monkeypatched version of that flow.
"""

import pytest


def test_auth_login_page_is_reachable(client):
    resp = client.get("/auth/login", auth=False)
    assert resp.status == 200


@pytest.mark.parametrize("prefix", ["/api/sessions", "/otlp/v1/logs"])
def test_protected_prefix_rejects_missing_token(client, prefix):
    resp = client._request("GET", prefix, auth=False)
    assert resp.status == 401
    # mcp_server/middleware.py sets WWW-Authenticate on every 401 from
    # MultiTokenAuthMiddleware specifically so MCP clients can do RFC
    # 9728 OAuth discovery. Absence here would mean that discovery path
    # is broken, not just "some header is missing".
    assert "www-authenticate" in {k.lower() for k in resp.headers}


def test_mcp_get_with_no_token_is_the_documented_anonymous_sse_carveout(client):
    # GET /mcp with no token is intentionally NOT gated: it's the
    # client opening the server->client SSE stream, and
    # MultiTokenAuthMiddleware's _is_anon_mcp_ok carve-out lets that
    # specific case through unauthenticated (bound to
    # ANON_DISCOVERY_OWNER, which sees no session data). So this
    # never reaches the 401 branch at all; it reaches the MCP SDK's
    # own streamable_http_app, which then rejects it on its own
    # grounds (missing Accept: text/event-stream, or once that's
    # supplied, a missing session ID). Confirming that reachable,
    # not-401, SDK-level rejection here catches a real regression
    # if the carve-out is ever removed or narrowed, without asserting
    # a 401 this path was never supposed to produce.
    resp = client._request("GET", "/mcp", auth=False)
    assert resp.status == 406


@pytest.mark.parametrize("prefix", ["/api/sessions", "/otlp/v1/logs", "/mcp"])
def test_protected_prefix_rejects_invalid_token(client, prefix):
    resp = client._request("GET", prefix, headers={"Authorization": "Bearer definitely-not-a-real-token"}, auth=False)
    assert resp.status == 401
