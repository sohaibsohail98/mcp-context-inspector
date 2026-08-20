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


def test_root_redirects_to_auth_login(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302, 307, 308)
    assert resp.headers["location"] == "/auth/login"


def test_healthz_is_unauthenticated_and_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_maybe_seed_demo_db_copies_when_missing(tmp_path):
    src = tmp_path / "demo.db"
    src.write_bytes(b"fake-db-contents")
    target = tmp_path / "runtime" / "metrics.db"

    copied = server_module._maybe_seed_demo_db(str(src), target)

    assert copied is True
    assert target.read_bytes() == b"fake-db-contents"


def test_maybe_seed_demo_db_noop_without_src():
    target_that_would_error_if_touched = None  # target.exists() would crash on None; src check short-circuits first
    assert server_module._maybe_seed_demo_db(None, target_that_would_error_if_touched) is False


def test_resolve_owner_token_strips_trailing_newline():
    """Secret Manager values created via `echo | gcloud secrets create`
    commonly carry a trailing newline that becomes part of the mounted
    env var — an untrimmed comparison would reject the real token."""
    assert server_module._resolve_owner_token("real-token\n") == "real-token"


def test_resolve_owner_token_none_for_blank():
    assert server_module._resolve_owner_token(None) is None
    assert server_module._resolve_owner_token("") is None
    assert server_module._resolve_owner_token("   \n") is None


def test_maybe_seed_demo_db_noop_when_target_exists(tmp_path):
    src = tmp_path / "demo.db"
    src.write_bytes(b"fake-db-contents")
    target = tmp_path / "metrics.db"
    target.write_bytes(b"already-here")

    copied = server_module._maybe_seed_demo_db(str(src), target)

    assert copied is False
    assert target.read_bytes() == b"already-here"


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


def _basic_loop_result():
    return {
        "trace": [],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }


def test_api_record_session_attributes_to_the_calling_token(client, isolated_auth_store):
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    client.post(
        "/api/record-session",
        json={"prompt": "alice's q", "model_id": "m", "loop_result": _basic_loop_result()},
        headers={"Authorization": f"Bearer {alice_token}"},
    )

    alice_view = client.get("/api/sessions", headers={"Authorization": f"Bearer {alice_token}"}).json()
    bob_view = client.get("/api/sessions", headers={"Authorization": f"Bearer {bob_token}"}).json()
    owner_view = client.get("/api/sessions", headers={"Authorization": "Bearer owner-secret"}).json()

    assert len(alice_view) == 1
    assert alice_view[0]["prompt"] == "alice's q"
    assert bob_view == []
    assert len(owner_view) == 1


def test_api_record_session_missing_field_is_a_400(client):
    resp = client.post(
        "/api/record-session",
        json={"prompt": "q", "model_id": "m"},  # missing loop_result
        headers={"Authorization": "Bearer owner-secret"},
    )
    assert resp.status_code == 400


def test_api_record_session_requires_auth(client):
    resp = client.post(
        "/api/record-session",
        json={"prompt": "q", "model_id": "m", "loop_result": _basic_loop_result()},
    )
    assert resp.status_code == 401


def test_api_record_session_malformed_json_body_is_a_400_not_a_500(client):
    """request.json() on a non-JSON body must not raise an unhandled
    JSONDecodeError — a malformed request from any client (or any
    friend's agent) should get a clean, documented 400, not an
    internal server error."""
    resp = client.post(
        "/api/record-session",
        content=b"not json at all",
        headers={"Authorization": "Bearer owner-secret", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_api_record_session_non_object_json_body_is_a_400(client):
    resp = client.post(
        "/api/record-session",
        content=b"[1, 2, 3]",
        headers={"Authorization": "Bearer owner-secret", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_auth_verify_malformed_json_body_is_a_400_not_a_500(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    resp = client.post(
        "/auth/verify",
        content=b"not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_auth_verify_non_object_json_body_is_a_400(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    resp = client.post(
        "/auth/verify",
        content=b'"just a string"',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


_PROTECTED_ROUTES = [
    ("GET", "/api/sessions"),
    ("GET", "/api/sessions/some-id"),
    ("GET", "/api/tool-metrics"),
    ("GET", "/api/cost"),
    ("GET", "/api/context-timeline/some-id"),
    ("POST", "/api/record-session"),
]


@pytest.mark.parametrize("method,path", _PROTECTED_ROUTES)
def test_every_protected_route_rejects_garbage_token(client, method, path):
    """A sweep, not a sample — every /api/* route must reject an
    unrecognized bearer token, not just the ones covered by earlier
    spot-checks. Adding a new route without wiring it into
    MultiTokenAuthMiddleware's prefix match would otherwise ship
    silently open."""
    resp = client.request(
        method, path, json={} if method == "POST" else None,
        headers={"Authorization": "Bearer complete-nonsense-token"},
    )
    assert resp.status_code == 401


@pytest.mark.parametrize("method,path", _PROTECTED_ROUTES)
def test_every_protected_route_rejects_missing_token(client, method, path):
    resp = client.request(method, path, json={} if method == "POST" else None)
    assert resp.status_code == 401


def test_otlp_logs_route_rejects_missing_token(client):
    """/otlp/* must be gated exactly like /api/* — it returns the same
    session/token data, just via a different ingestion path."""
    resp = client.post("/otlp/v1/logs", json={"resourceLogs": []})
    assert resp.status_code == 401


def test_otlp_logs_route_accepts_owner_token(client):
    resp = client.post(
        "/otlp/v1/logs",
        json={"resourceLogs": []},
        headers={"Authorization": "Bearer owner-secret"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"accepted": {"claude_code": 0, "copilot": 0, "skipped": 0}}


def test_otlp_metrics_route_accepts_valid_per_user_token(client, isolated_auth_store):
    token = isolated_auth_store.get_or_create_token("sub123", "a@example.com")
    resp = client.post(
        "/otlp/v1/metrics",
        json={"resourceMetrics": []},
        headers={"Authorization": "Bearer " + token},
    )
    assert resp.status_code == 200


def test_otlp_traces_route_rejects_wrong_token(client):
    resp = client.post(
        "/otlp/v1/traces",
        json={"resourceSpans": []},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


def test_otlp_route_rejects_malformed_json_body(client):
    resp = client.post(
        "/otlp/v1/logs",
        content=b"not json",
        headers={"Authorization": "Bearer owner-secret", "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_otlp_route_rejects_non_object_body(client):
    resp = client.post(
        "/otlp/v1/logs",
        json=["not", "an", "object"],
        headers={"Authorization": "Bearer owner-secret"},
    )
    assert resp.status_code == 400
