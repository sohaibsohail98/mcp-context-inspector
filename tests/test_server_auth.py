"""Regression tests for MultiTokenAuthMiddleware and the /auth/login,
/auth/verify routes, in-process via Starlette's TestClient, no real
subprocess, no real network call to Google (verify_credential is
monkeypatched at the mcp_server.routes_auth module level, where
auth_verify's route handler actually looks it up).
"""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from mcp_server import local_setup
from mcp_server import server as server_module
from mcp_server.auth.google import InvalidGoogleToken
from mcp_server.routes import auth as routes_auth


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db):
    """A fresh app per test, gated by MultiTokenAuthMiddleware with a
    fixed owner token. isolated_auth_store/isolated_sqlite_db mean
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
    env var, and an untrimmed comparison would reject the real token."""
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
    """The pre-auth sign-in page itself must not require a token:
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
        routes_auth, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "a@example.com"
    assert isolated_auth_store.is_valid_token(body["mcp_token"])


def test_auth_verify_is_unauthenticated_itself(client, monkeypatch):
    """Verifying doesn't require an existing MCP token; you're getting
    your FIRST token here."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        routes_auth, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
    )
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code != 401


def test_auth_verify_rejects_invalid_google_credential(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")

    def _raise(credential, client_id):
        raise InvalidGoogleToken("bad signature")

    monkeypatch.setattr(routes_auth, "verify_credential", _raise)
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
        routes_auth, "verify_credential", lambda credential, client_id: {"sub": "sub123", "email": "a@example.com"}
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


# --- Developer mode: hiding api_tests probe sessions ---------------------
# api_tests/ posts real OTLP payloads against a real deployment to verify
# ingestion end-to-end (see api_tests/README.md), and every session_id it
# creates is prefixed "api-tests-". Without filtering, every account's
# dashboard would show these synthetic probe sessions alongside real ones.


def _claude_code_otlp_payload(session_id):
    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "attributes": [
                                    {"key": "session.id", "value": {"stringValue": session_id}},
                                    # A real turn event, not just a bare log record. See
                                    # otlp/claude_code.py's _TURN_EVENT_NAMES: a session_id
                                    # with no turn-shaped event (e.g. only
                                    # mcp_server_connection) doesn't create a session row.
                                    {"key": "event.name", "value": {"stringValue": "user_prompt"}},
                                ],
                                "body": {"stringValue": "api_tests probe"},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_api_sessions_hides_api_tests_probe_sessions_by_default(client):
    real_id = "sess_real123"
    test_id = "api-tests-abc123"
    client.post(
        "/otlp/v1/logs", json=_claude_code_otlp_payload(real_id), headers={"Authorization": "Bearer owner-secret"}
    )
    client.post(
        "/otlp/v1/logs", json=_claude_code_otlp_payload(test_id), headers={"Authorization": "Bearer owner-secret"}
    )

    sessions = client.get("/api/sessions", headers={"Authorization": "Bearer owner-secret"}).json()
    session_ids = {s["session_id"] for s in sessions}
    assert real_id in session_ids
    assert test_id not in session_ids


def test_dev_mode_status_true_for_owner_token(client):
    resp = client.get("/api/dev-mode-status", headers={"Authorization": "Bearer owner-secret"})
    assert resp.json() == {"dev_mode": True}


def test_dev_mode_status_false_for_unlisted_per_user_token(client, isolated_auth_store):
    token = isolated_auth_store.get_or_create_token("not-on-allowlist-sub", "a@example.com")
    resp = client.get("/api/dev-mode-status", headers={"Authorization": f"Bearer {token}"})
    assert resp.json() == {"dev_mode": False}


def test_dev_mode_status_true_for_allowlisted_sub(client, isolated_auth_store, monkeypatch):
    monkeypatch.setenv("DEV_MODE_SUBS", "friend-sub,another-sub")
    token = isolated_auth_store.get_or_create_token("friend-sub", "friend@example.com")
    resp = client.get("/api/dev-mode-status", headers={"Authorization": f"Bearer {token}"})
    assert resp.json() == {"dev_mode": True}


def test_include_test_sessions_ignored_for_non_dev_mode_account(client, isolated_auth_store):
    """A stray ?include_test_sessions=1 from an unlisted account must be
    silently ignored, not honored; otherwise the query param itself
    becomes a way to bypass the allowlist."""
    token = isolated_auth_store.get_or_create_token("not-on-allowlist-sub", "a@example.com")
    test_id = "api-tests-xyz789"
    client.post(
        "/otlp/v1/logs",
        json=_claude_code_otlp_payload(test_id),
        headers={"Authorization": f"Bearer {token}"},
    )

    sessions = client.get(
        "/api/sessions?include_test_sessions=1", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert test_id not in {s["session_id"] for s in sessions}


def test_include_test_sessions_honored_for_owner_token(client):
    real_id = "sess_real456"
    test_id = "api-tests-def456"
    client.post(
        "/otlp/v1/logs", json=_claude_code_otlp_payload(real_id), headers={"Authorization": "Bearer owner-secret"}
    )
    client.post(
        "/otlp/v1/logs", json=_claude_code_otlp_payload(test_id), headers={"Authorization": "Bearer owner-secret"}
    )

    sessions = client.get(
        "/api/sessions?include_test_sessions=1", headers={"Authorization": "Bearer owner-secret"}
    ).json()
    session_ids = {s["session_id"] for s in sessions}
    assert real_id in session_ids
    assert test_id in session_ids


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
    JSONDecodeError. A malformed request from any client (or any
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
    """A sweep, not a sample: every /api/* route must reject an
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
    """/otlp/* must be gated exactly like /api/*, since it returns the same
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


# --- /otlp/debug tenant isolation ---------------------------------------
# Regression coverage for the gap the launch-plan review found: this route
# used to return process-global counters, not filtered by current_owner,
# see mcp_server/otlp/__init__.py's owner-keyed _counts.


def test_otlp_debug_does_not_leak_another_tenants_data(client, isolated_auth_store):
    """Alice's session landing must not flip Bob's /otlp/debug panel to
    "connected": the exact false-positive the review flagged."""
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    resource_logs = {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                "scopeLogs": [{"logRecords": [{"body": {"stringValue": "hi"}}]}],
            }
        ]
    }
    client.post("/otlp/v1/logs", json=resource_logs, headers={"Authorization": f"Bearer {alice_token}"})

    alice_debug = client.get("/otlp/debug", headers={"Authorization": f"Bearer {alice_token}"}).json()
    bob_debug = client.get("/otlp/debug", headers={"Authorization": f"Bearer {bob_token}"}).json()

    assert alice_debug["counts"]["claude_code"] == 1
    assert alice_debug["last_accepted_at"]["claude_code"] is not None
    assert bob_debug["counts"]["claude_code"] == 0
    assert bob_debug["last_accepted_at"]["claude_code"] is None


def test_otlp_debug_recent_skipped_is_owner_scoped(client, isolated_auth_store):
    """recent_skipped carries resource_attrs (hostnames, session IDs),
    the review's second finding was that any authenticated caller could
    read another tenant's entries here."""
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    undetected_vendor_logs = {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "host.name", "value": {"stringValue": "alices-laptop"}}]},
                "scopeLogs": [{"logRecords": [{"body": {"stringValue": "hi"}}]}],
            }
        ]
    }
    client.post(
        "/otlp/v1/logs", json=undetected_vendor_logs, headers={"Authorization": f"Bearer {alice_token}"}
    )

    alice_debug = client.get("/otlp/debug", headers={"Authorization": f"Bearer {alice_token}"}).json()
    bob_debug = client.get("/otlp/debug", headers={"Authorization": f"Bearer {bob_token}"}).json()

    assert len(alice_debug["recent_skipped"]) == 1
    assert alice_debug["recent_skipped"][0]["resource_attrs"]["host.name"] == "alices-laptop"
    assert bob_debug["recent_skipped"] == []


def test_otlp_debug_owner_token_has_its_own_bucket(client, isolated_auth_store):
    """The shared owner token's counters are their own bucket, not a
    merge of every signed-in user's data."""
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    client.post(
        "/otlp/v1/logs",
        json={"resourceLogs": []},
        headers={"Authorization": f"Bearer {alice_token}"},
    )

    owner_debug = client.get("/otlp/debug", headers={"Authorization": "Bearer owner-secret"}).json()
    assert owner_debug["counts"] == {"claude_code": 0, "copilot": 0, "skipped": 0}


# --- /setup/apply-local-config -----------------------------------------
# This route does a real local filesystem write (the caller's own
# ~/.claude/settings.json), so every test here monkeypatches
# server_module._CLAUDE_SETTINGS_PATH to a tmp_path file, never the real
# path, and uses a loopback-address TestClient (Starlette's TestClient
# defaults its "client" to ("testclient", 50000), not a loopback address,
# which is itself the thing the non-loopback rejection test relies on).


@pytest.fixture
def loopback_client(isolated_auth_store, isolated_sqlite_db):
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app, client=("127.0.0.1", 54321)) as c:
        yield c


def test_apply_local_config_rejects_non_loopback(client):
    """The default `client` fixture's TestClient host is "testclient",
    not a loopback address: exactly the case this endpoint must reject,
    since it performs a real local filesystem write."""
    resp = client.post("/setup/apply-local-config", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 403


def test_apply_local_config_rejects_missing_token(loopback_client):
    resp = loopback_client.post("/setup/apply-local-config")
    assert resp.status_code == 401


def test_apply_local_config_creates_new_file(loopback_client, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(local_setup, "SETTINGS_PATH", settings_path)

    resp = loopback_client.post("/setup/apply-local-config", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["backed_up_to"] is None  # nothing existed to back up

    written = json.loads(settings_path.read_text())
    assert written["mcpServers"]["context-inspector"]["headers"]["Authorization"] == "Bearer owner-secret"
    assert written["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert written["env"]["CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH"] == "1048576"


def test_apply_local_config_merges_without_clobbering_existing_entries(loopback_client, tmp_path, monkeypatch):
    """An existing, unrelated mcpServers entry and env var must survive,
    this route merges into the file, it never replaces it wholesale."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps({
        "effortLevel": "medium",
        "mcpServers": {"lockin": {"url": "https://lockin.example/mcp", "headers": {"Authorization": "Bearer lin_x"}}},
        "env": {"SOME_OTHER_VAR": "keep-me"},
    }))
    monkeypatch.setattr(local_setup, "SETTINGS_PATH", settings_path)

    resp = loopback_client.post("/setup/apply-local-config", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["backed_up_to"] is not None
    assert Path(body["backed_up_to"]).exists()

    written = json.loads(settings_path.read_text())
    assert written["effortLevel"] == "medium"
    assert written["mcpServers"]["lockin"]["url"] == "https://lockin.example/mcp"
    assert written["mcpServers"]["context-inspector"]["headers"]["Authorization"] == "Bearer owner-secret"
    assert written["env"]["SOME_OTHER_VAR"] == "keep-me"
    assert written["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"


def test_apply_local_config_refuses_to_touch_invalid_json(loopback_client, tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text("not valid json {{{")
    monkeypatch.setattr(local_setup, "SETTINGS_PATH", settings_path)

    resp = loopback_client.post("/setup/apply-local-config", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 409
    assert settings_path.read_text() == "not valid json {{{"  # untouched


# --- /setup/local-script -------------------------------------------------
# The deployed-instance counterpart to /setup/apply-local-config: instead
# of writing settings.json itself (no filesystem access to the caller's
# machine), it hands back a personalized script that does the same write
# when the user runs it locally. Available from any host, not just
# loopback. The whole point is it works for a deployed instance.


def test_local_script_rejects_missing_token(client):
    resp = client.get("/setup/local-script")
    assert resp.status_code == 401


def test_local_script_available_on_non_loopback_host(client):
    """Unlike /setup/apply-local-config, this route must NOT be gated to
    loopback requests. A deployed instance is exactly the case it exists
    for."""
    resp = client.get("/setup/local-script", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200


def test_local_script_is_a_downloadable_attachment(client):
    resp = client.get("/setup/local-script", headers={"Authorization": "Bearer owner-secret"})
    assert "attachment" in resp.headers["content-disposition"]
    assert resp.headers["content-type"].startswith("text/plain")


def test_local_script_embeds_token_and_base_url(client):
    resp = client.get("/setup/local-script", headers={"Authorization": "Bearer owner-secret"})
    body = resp.text
    assert "owner-secret" in body
    assert "/mcp" in body
    assert "/otlp" in body


def test_local_script_is_valid_python(client):
    import ast

    resp = client.get("/setup/local-script", headers={"Authorization": "Bearer owner-secret"})
    ast.parse(resp.text)  # raises SyntaxError if the template substitution broke anything


# --- /setup/issue-install-code + /setup/install --------------------------
# The curl-able one-liner: /setup/issue-install-code mints a short-lived,
# single-use code bound to the caller's own bearer token; /setup/install
# exchanges that code (no Authorization header; the code IS the
# credential, since a piped curl command can't carry a bearer header) for
# a POSIX-sh installer script that performs the identical settings.json
# merge as /setup/local-script's downloaded Python script.


def test_issue_install_code_rejects_missing_token(client):
    resp = client.post("/setup/issue-install-code")
    assert resp.status_code == 401


def test_issue_install_code_returns_code_and_ttl(client):
    resp = client.post("/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"]
    assert body["expires_in"] == local_setup.INSTALL_CODE_TTL_SECONDS


def test_setup_install_rejects_missing_code(client):
    resp = client.get("/setup/install")
    assert resp.status_code == 400


def test_setup_install_rejects_invalid_code(client):
    resp = client.get("/setup/install", params={"t": "not-a-real-code"})
    assert resp.status_code == 400


def test_setup_install_exchanges_valid_code_for_a_shell_script(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]

    resp = client.get("/setup/install", params={"t": code})
    assert resp.status_code == 200
    assert "owner-secret" in resp.text
    assert "/mcp" in resp.text
    assert "/otlp" in resp.text
    assert resp.text.startswith("#!/bin/sh")


def test_setup_install_code_is_single_use(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]

    first = client.get("/setup/install", params={"t": code})
    second = client.get("/setup/install", params={"t": code})
    assert first.status_code == 200
    assert second.status_code == 400


def test_setup_install_rejects_expired_code(client, monkeypatch, isolated_auth_store):
    from mcp_server.auth import store_sqlite

    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]

    # Simulate the "copied the command, got pulled away, pasted it 20
    # minutes later" case the plan explicitly calls out as the common
    # failure mode, so backdate expires_at directly rather than sleeping.
    conn = store_sqlite._connect()
    conn.execute("UPDATE install_codes SET expires_at = 0")
    conn.commit()
    conn.close()

    resp = client.get("/setup/install", params={"t": code})
    assert resp.status_code == 400
    assert "expired" in resp.text.lower()


def test_setup_install_script_is_valid_posix_shell(client):
    """A malformed template (unbalanced braces from the heavy {{ }}
    escaping) would otherwise only surface at curl-time in someone's
    terminal, so catch it here instead by actually invoking `sh -n`
    (syntax-check only, no execution) against the real script."""
    import subprocess

    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    script = client.get("/setup/install", params={"t": code}).text

    result = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "malicious_token",
    [
        "ab$HOME-cd",  # shell variable expansion
        "ab`id`cd",  # command substitution (backticks)
        "ab$(id)cd",  # command substitution (modern form)
        r"ab\$HOME-cd",  # literal backslash
    ],
)
def test_setup_install_script_does_not_shell_expand_the_token(client, tmp_path, monkeypatch, malicious_token):
    """Regression test for a shell-injection bug found in review: the
    installer used to interpolate BEARER_TOKEN into a double-quoted
    `python3 -c "..."` body, so a token containing `$`, backticks, or a
    backslash was expanded/executed by `sh` before Python ever saw it.
    confirmed to both silently corrupt the stored token (breaking every
    OTLP export with an unexplained 401) and, for the backtick case,
    execute arbitrary shell commands during install. The owner token
    (MCP_AUTH_TOKEN) is operator-set and not guaranteed to avoid these
    characters, unlike per-user tokens minted via secrets.token_urlsafe.
    The fix passes the token through an env var with real POSIX sh
    single-quoting, never through double-quoted string interpolation."""
    import subprocess

    from mcp_server.auth import store_sqlite

    # Bypass the normal issue/redeem-code flow to control the exact
    # bearer token value embedded in the script. The malicious payload
    # needs to be the raw bearer token, not something that goes through
    # JSON encoding first.
    code = store_sqlite.issue_install_code(malicious_token)
    script = client.get("/setup/install", params={"t": code}).text

    result = subprocess.run(["sh", "-n"], input=script, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    script_path = tmp_path / "install.sh"
    script_path.write_text(script)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = subprocess.run(["sh", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    stored_auth_header = written["mcpServers"]["context-inspector"]["headers"]["Authorization"]
    assert stored_auth_header == "Bearer " + malicious_token


def test_setup_install_script_execution_applies_same_patch_as_local_script(client, tmp_path, monkeypatch):
    """The curl-installer must perform the identical settings.json merge
    as /setup/local-script's downloaded Python script (same keys, same
    values), since it's meant to be an equally valid path to the same
    result."""
    import subprocess

    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    script = client.get("/setup/install", params={"t": code}).text

    script_path = tmp_path / "install.sh"
    script_path.write_text(script)
    monkeypatch.setenv("HOME", str(tmp_path))

    result = subprocess.run(["sh", str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert written["mcpServers"]["context-inspector"]["headers"]["Authorization"] == "Bearer owner-secret"
    assert written["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert written["env"]["OTEL_RESOURCE_ATTRIBUTES"] == "service.name=claude-code"


# --- /setup/install PowerShell variant (?os=windows) --------------------


def test_setup_install_serves_powershell_for_os_windows(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    resp = client.get("/setup/install", params={"t": code, "os": "windows"})
    assert resp.status_code == 200
    assert "owner-secret" in resp.text
    assert "/mcp" in resp.text and "/otlp" in resp.text
    assert "$env:MCP_INSTALL_BEARER_TOKEN" in resp.text
    assert not resp.text.startswith("#!/bin/sh")
    assert "text/plain" in resp.headers["content-type"]
    assert resp.headers.get("cache-control") == "no-store"


def test_setup_install_still_serves_sh_without_os_param(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    resp = client.get("/setup/install", params={"t": code})
    assert resp.status_code == 200
    assert resp.text.startswith("#!/bin/sh")


def test_setup_install_powershell_still_requires_a_valid_code(client):
    resp = client.get("/setup/install", params={"t": "bogus", "os": "windows"})
    assert resp.status_code == 400
    assert "Write-Error" in resp.text  # PS-flavoured error, not `echo ... >&2`


def test_setup_install_powershell_code_is_single_use(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    first = client.get("/setup/install", params={"t": code, "os": "windows"})
    second = client.get("/setup/install", params={"t": code, "os": "windows"})
    assert first.status_code == 200
    assert second.status_code == 400


def test_setup_install_ua_sniff_defaults_powershell(client):
    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    resp = client.get(
        "/setup/install",
        params={"t": code},
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0) WindowsPowerShell/5.1.19041.1"},
    )
    assert not resp.text.startswith("#!/bin/sh")
    assert "$env:MCP_INSTALL_BEARER_TOKEN" in resp.text


def test_setup_install_powershell_embedded_python_applies_same_patch(client, tmp_path, monkeypatch):
    """The PowerShell script's embedded `python -c` body must perform the
    identical settings.json merge as the sh installer (it is the same
    body). Extract and run it directly (no PowerShell needed on CI)."""
    import subprocess
    import sys

    code = client.post(
        "/setup/issue-install-code", headers={"Authorization": "Bearer owner-secret"}
    ).json()["code"]
    script = client.get("/setup/install", params={"t": code, "os": "windows"}).text

    body = script.split('& $py -c "', 1)[1].rsplit('\n"\n', 1)[0]
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("MCP_INSTALL_BASE_URL", "https://ctxwindow.uk")
    monkeypatch.setenv("MCP_INSTALL_BEARER_TOKEN", "owner-secret")
    result = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert written["mcpServers"]["context-inspector"]["url"] == "https://ctxwindow.uk/mcp"
    assert written["mcpServers"]["context-inspector"]["headers"]["Authorization"] == "Bearer owner-secret"
    assert written["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"

    # idempotent: a second run backs up (merge, not overwrite) and leaves
    # the same result
    result2 = subprocess.run([sys.executable, "-c", body], capture_output=True, text=True)
    assert result2.returncode == 0, result2.stderr
    assert list((tmp_path / ".claude").glob("settings.json.bak-*"))


def test_render_powershell_single_quote_escaping():
    from mcp_server import local_setup

    out = local_setup.render_install_powershell_script("https://x.uk", "a'b")
    assert "'a''b'" in out  # PowerShell doubled-quote escaping
    assert "https://x.uk" in out


def test_local_script_execution_applies_same_patch_as_apply_local_config(client, tmp_path, monkeypatch):
    """The downloaded script must perform the identical settings.json
    merge as the in-process route (same keys, same values), since it's
    meant to be a drop-in substitute for users who can't reach the
    loopback-only route."""
    resp = client.get("/setup/local-script", headers={"Authorization": "Bearer owner-secret"})
    script_path = tmp_path / "setup.py"
    script_path.write_text(resp.text)

    monkeypatch.setenv("HOME", str(tmp_path))
    # The script resolves ~/.claude/settings.json via Path.home(); point
    # HOME at tmp_path and expect the write under tmp_path/.claude/.
    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(script_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    written = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert written["mcpServers"]["context-inspector"]["headers"]["Authorization"] == "Bearer owner-secret"
    assert written["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert written["env"]["CLAUDE_CODE_OTEL_CONTENT_MAX_LENGTH"] == "1048576"


# --- /auth/devices and /auth/revoke-device ---------------------------

def _sign_in(client, monkeypatch, sub="sub123", email="a@example.com"):
    """Drive the real /auth/verify flow and return the minted per-device
    token (TestClient sends a constant User-Agent, so this is stable)."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(
        routes_auth, "verify_credential", lambda credential, client_id: {"sub": sub, "email": email}
    )
    resp = client.post("/auth/verify", json={"credential": "fake-jwt"})
    assert resp.status_code == 200
    return resp.json()["mcp_token"]


def test_auth_devices_requires_a_valid_token(client):
    assert client.get("/auth/devices").status_code == 401
    assert client.get("/auth/devices", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_auth_devices_lists_the_callers_own_devices(client, monkeypatch, isolated_auth_store):
    token = _sign_in(client, monkeypatch)
    resp = client.get("/auth/devices", headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 200
    devices = resp.json()["devices"]
    assert len(devices) == 1
    assert devices[0]["is_current"] is True
    assert devices[0]["label"]  # some label, never blank
    # never leaks the raw token
    assert token not in str(devices)


def test_auth_devices_only_shows_your_own(client, monkeypatch, isolated_auth_store):
    alice_token = _sign_in(client, monkeypatch, sub="alice", email="alice@example.com")
    # A second device for alice, minted directly with a different UA.
    isolated_auth_store.get_or_create_device_token("alice", "alice@example.com", "curl/8.0")
    isolated_auth_store.get_or_create_device_token("bob", "bob@example.com", "curl/8.0")

    devices = client.get("/auth/devices", headers={"Authorization": "Bearer " + alice_token}).json()["devices"]
    assert len(devices) == 2  # alice's two, not bob's


def test_auth_revoke_device_requires_a_valid_token(client):
    assert client.post("/auth/revoke-device", json={"token_id": "x"}).status_code == 401


def test_auth_revoke_device_revokes_one_and_only_one(client, monkeypatch, isolated_auth_store):
    current = _sign_in(client, monkeypatch)
    other = isolated_auth_store.get_or_create_device_token("sub123", "a@example.com", "curl/8.0")
    devices = client.get("/auth/devices", headers={"Authorization": "Bearer " + current}).json()["devices"]
    other_id = next(d["token_id"] for d in devices if not d["is_current"])

    resp = client.post(
        "/auth/revoke-device",
        headers={"Authorization": "Bearer " + current},
        json={"token_id": other_id},
    )
    assert resp.status_code == 200
    assert isolated_auth_store.is_valid_token(other) is False
    assert isolated_auth_store.is_valid_token(current) is True


def test_auth_revoke_device_cannot_revoke_another_users_token(client, monkeypatch, isolated_auth_store):
    alice_token = _sign_in(client, monkeypatch, sub="alice", email="alice@example.com")
    bob_device = isolated_auth_store.get_or_create_device_token("bob", "bob@example.com", "curl/8.0")
    bob_id = isolated_auth_store.list_tokens("bob")[0]["token_id"]

    resp = client.post(
        "/auth/revoke-device",
        headers={"Authorization": "Bearer " + alice_token},
        json={"token_id": bob_id},
    )
    assert resp.status_code == 200  # scoped query matched nothing, still a clean 200
    assert isolated_auth_store.is_valid_token(bob_device) is True


def test_auth_revoke_device_is_idempotent(client, monkeypatch, isolated_auth_store):
    token = _sign_in(client, monkeypatch)
    dev_id = isolated_auth_store.list_tokens("sub123")[0]["token_id"]
    first = client.post("/auth/revoke-device", headers={"Authorization": "Bearer " + token}, json={"token_id": dev_id})
    # token is now revoked, so a second call authenticates with a dead token -> 401
    assert first.status_code == 200
    second = client.post("/auth/revoke-device", headers={"Authorization": "Bearer " + token}, json={"token_id": dev_id})
    assert second.status_code == 401


def test_auth_revoke_device_missing_token_id_is_a_400(client, monkeypatch, isolated_auth_store):
    token = _sign_in(client, monkeypatch)
    resp = client.post("/auth/revoke-device", headers={"Authorization": "Bearer " + token}, json={})
    assert resp.status_code == 400
