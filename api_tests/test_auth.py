"""Auth surface: /auth/login reachability, and that every protected
prefix (mcp_server/middleware.py's MultiTokenAuthMiddleware) actually
rejects a missing/invalid bearer token. Does NOT exercise /auth/verify's
real Google-credential exchange — that needs a live Google OAuth
round-trip, not something a black-box HTTP test can drive without a
real browser/user. See tests/test_oauth.py for the in-process,
monkeypatched version of that flow.
"""

import pytest


def test_auth_login_page_is_reachable(client):
    resp = client.get("/auth/login", auth=False)
    assert resp.status == 200


@pytest.mark.parametrize("prefix", ["/api/sessions", "/otlp/v1/logs", "/mcp"])
def test_protected_prefix_rejects_missing_token(client, prefix):
    resp = client._request("GET", prefix, auth=False)
    assert resp.status == 401
    # mcp_server/middleware.py sets WWW-Authenticate on every 401 from
    # MultiTokenAuthMiddleware specifically so MCP clients can do RFC
    # 9728 OAuth discovery — absence here would mean that discovery path
    # is broken, not just "some header is missing".
    assert "www-authenticate" in {k.lower() for k in resp.headers}


@pytest.mark.parametrize("prefix", ["/api/sessions", "/otlp/v1/logs", "/mcp"])
def test_protected_prefix_rejects_invalid_token(client, prefix):
    resp = client._request(
        "GET", prefix, headers={"Authorization": "Bearer definitely-not-a-real-token"}, auth=False
    )
    assert resp.status == 401
