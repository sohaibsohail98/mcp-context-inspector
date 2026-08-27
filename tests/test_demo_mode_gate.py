"""CTXWINDOW_DEMO_MODE and ?demo=1 must both be present before the
staged-reveal script is ever referenced, and the demo bearer token must
only be accepted with the env var on. Guards against the demo capture
pipeline (scripts/demo_capture.py) accidentally becoming reachable, or
having any visible effect, on a real deployment."""

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module
from mcp_server.middleware import DEMO_TOKEN


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db):
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app) as c:
        yield c


def test_demo_script_absent_without_either_gate(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.delenv("CTXWINDOW_DEMO_MODE", raising=False)
    assert "demo_reveal.js" not in client.get("/auth/login").text
    assert "demo_reveal.js" not in client.get("/auth/login?demo=1").text


def test_demo_script_present_only_with_both_gates(client, monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("CTXWINDOW_DEMO_MODE", "1")
    assert "demo_reveal.js" not in client.get("/auth/login").text
    assert "demo_reveal.js" in client.get("/auth/login?demo=1").text


def test_demo_token_rejected_without_env_var(client, monkeypatch):
    monkeypatch.delenv("CTXWINDOW_DEMO_MODE", raising=False)
    resp = client.get("/api/sessions", headers={"Authorization": f"Bearer {DEMO_TOKEN}"})
    assert resp.status_code == 401


def test_demo_token_accepted_with_env_var(client, monkeypatch):
    monkeypatch.setenv("CTXWINDOW_DEMO_MODE", "1")
    resp = client.get("/api/sessions", headers={"Authorization": f"Bearer {DEMO_TOKEN}"})
    assert resp.status_code == 200


def test_demo_token_sees_owner_scoped_sessions(client, monkeypatch, isolated_auth_store):
    """The seeded demo dataset (scripts/seed_demo_db.py) inserts rows
    with owner=NULL, same as the shared owner token; the demo token must
    resolve to that same scope, not its own isolated (and therefore
    empty) owner bucket."""
    monkeypatch.setenv("CTXWINDOW_DEMO_MODE", "1")
    client.post(
        "/api/record-session",
        json={
            "prompt": "owner-scoped session",
            "model_id": "m",
            "loop_result": {
                "trace": [],
                "turns": [{"input_tokens": 1, "output_tokens": 1, "latency_ms": 1}],
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "latency_ms": 1,
            },
        },
        headers={"Authorization": "Bearer owner-secret"},
    )
    resp = client.get("/api/sessions", headers={"Authorization": f"Bearer {DEMO_TOKEN}"})
    assert resp.status_code == 200
    assert any(s["prompt"] == "owner-scoped session" for s in resp.json())
