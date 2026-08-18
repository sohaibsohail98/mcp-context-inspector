"""Regression tests for MultiTokenAuthMiddleware and the /auth/login,
/auth/verify routes — in-process via Starlette's TestClient, no real
subprocess, no real network call to Google (verify_credential is
monkeypatched at the mcp_server.server module level, where auth_verify's
route handler actually looks it up).
"""

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module
from mcp_server.google_auth import InvalidGoogleToken


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db):
    """A fresh app per test, gated by MultiTokenAuthMiddleware with a
    fixed owner token — isolated_auth_store/isolated_sqlite_db mean
    neither per-user tokens nor session data leak between tests, or
    touch the developer's real local data/*.db files."""
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app) as c:
        yield c


def test_api_route_rejects_missing_token(client):
    resp = client.get("/api/sessions")
    assert resp.status_code == 401


def test_api_route_rejects_wrong_token(client):
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_api_route_accepts_owner_token(client):
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200


def test_api_route_accepts_valid_per_user_token(client, isolated_auth_store):
    token = isolated_auth_store.get_or_create_token("sub123", "a@example.com")
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 200


def test_api_route_rejects_revoked_per_user_token(client, isolated_auth_store):
    token = isolated_auth_store.get_or_create_token("sub123", "a@example.com")
    isolated_auth_store.revoke("sub123")
    resp = client.get("/api/sessions", headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 401


def test_auth_login_is_unauthenticated(client):
    """The pre-auth sign-in page itself must not require a token —
    that would be circular."""
    resp = client.get("/auth/login")
    assert resp.status_code != 401


def test_auth_login_without_client_id_reports_unavailable(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    resp = client.get("/auth/login")
    assert resp.status_code == 503


def test_auth_login_with_client_id_serves_signin_page(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "test-client-id" in resp.text


def test_auth_verify_mints_token_for_valid_credential(client, monkeypatch, isolated_auth_store):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        server_module, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert isolated_auth_store.is_valid_token(body["mcp_token"])


def test_auth_verify_is_unauthenticated_itself(client, monkeypatch):
    """Verifying doesn't require an existing MCP token — you're getting
    your FIRST token here."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        server_module, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code != 401


def test_auth_verify_rejects_invalid_google_credential(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")

    def _raise(credential, client_id):
        raise InvalidGoogleToken("bad signature")

    monkeypatch.setattr(server_module, "verify_credential", _raise)
    resp = client.post("/auth/verify", json={"credential": "garbage"})
    assert resp.status_code == 401


def test_auth_verify_without_client_id_configured(client, monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code == 503


def test_auth_verify_missing_credential_body(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    resp = client.post("/auth/verify", json={})
    assert resp.status_code == 400


def test_signing_in_twice_returns_the_same_token_end_to_end(client, monkeypatch, isolated_auth_store):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        server_module, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    first = client.post("/auth/verify", json={"credential": "fake-jwt"}).json()["mcp_token"]
    second = client.post("/auth/verify", json={"credential": "fake-jwt"}).json()["mcp_token"]
    assert first == second
