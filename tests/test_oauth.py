"""Regression tests for the OAuth 2.1 + PKCE authorization server
(/.well-known/*, /oauth/*) — the path a Connector-style MCP client uses
when it can't just be handed a plain bearer token. Exercises the full
discovery -> register -> authorize -> token -> use sequence end to end via
TestClient, plus the individual failure modes (PKCE mismatch, replay,
redirect_uri mismatch, expiry, unknown client, resource mismatch) each in
isolation.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from mcp_server import auth_store, server as server_module


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        server_module, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
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
    and connected via OAuth should have two independent tokens — so
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
