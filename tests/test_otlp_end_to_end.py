"""End-to-end integration tests for the OTLP telemetry pipeline —
exercises the real HTTP layer (Starlette TestClient, in-process, no
mocks below the route handler) all the way from a raw OTLP POST body
through auth, the vendor mapper, the store, and back out through the
/api/* routes the dashboard's own JS calls. Unit tests elsewhere already
cover each layer (mapper, store, auth) in isolation with more edge
cases — this file's job is only to prove the layers are actually wired
together correctly as one pipeline, and that a Bedrock-agent session,
a Claude-Code-via-OTLP session, and a Copilot-via-OTLP session can all
coexist correctly under the same owner-scoped dashboard.
"""

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db):
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app) as c:
        yield c


def _auth(token="owner-secret"):
    return {"Authorization": "Bearer " + token}


def _claude_code_logs_payload(session_id="e2e-cc-1"):
    request_body = {
        "system": "You are a coding assistant.",
        "tools": [{"name": "Read", "description": "Read a file"}],
        "messages": [{"role": "user", "content": "Fix the flaky test"}],
    }
    response_body = {
        "content": [{"type": "text", "text": "I'll look into it."}],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 40,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }

    import json as _json

    def _attrs(event_name, body, **extra):
        # Real Claude Code puts the raw body in a `body` ATTRIBUTE, not
        # the LogRecord's own top-level `body` field — confirmed via live
        # capture, see claude_code.py's module docstring.
        attrs = [
            {"key": "event.name", "value": {"stringValue": event_name}},
            {"key": "session.id", "value": {"stringValue": session_id}},
            {"key": "body", "value": {"stringValue": _json.dumps(body)}},
        ]
        for k, v in extra.items():
            attrs.append({"key": k, "value": {"stringValue": str(v)}})
        return attrs

    return {
        "resourceLogs": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "claude-code"}}]},
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": "1000000000",
                                "attributes": _attrs("api_request_body", request_body),
                                "body": {"stringValue": "claude_code.api_request_body"},
                            },
                            {
                                "timeUnixNano": "2000000000",
                                "attributes": _attrs("api_response_body", response_body),
                                "body": {"stringValue": "claude_code.api_response_body"},
                            },
                        ]
                    }
                ],
            }
        ]
    }


def _copilot_traces_payload(trace_id="e2e-gh-trace-1"):
    import json as _json

    def _attr(key, value):
        return {"key": key, "value": {"stringValue": value}}

    input_messages = _json.dumps([{"role": "user", "content": [{"type": "text", "text": "Refactor this"}]}])
    output_messages = _json.dumps([{"role": "assistant", "content": [{"type": "text", "text": "Done"}]}])

    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "github-copilot"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": "span-invoke",
                                "name": "invoke_agent",
                                "attributes": [],
                            },
                            {
                                "traceId": trace_id,
                                "spanId": "span-chat",
                                "parentSpanId": "span-invoke",
                                "name": "chat",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1500000000",
                                "attributes": [
                                    _attr("gen_ai.input.messages", input_messages),
                                    _attr("gen_ai.output.messages", output_messages),
                                    _attr("gen_ai.usage.input_tokens", "80"),
                                    _attr("gen_ai.usage.output_tokens", "20"),
                                ],
                            },
                        ]
                    }
                ],
            }
        ]
    }


def test_claude_code_otlp_logs_flow_to_dashboard_api(client):
    """POST /otlp/v1/logs -> claude_code mapper -> store -> /api/sessions
    and /api/sessions/{id} both reflect the ingested session, with the
    right source badge and real token counts from the response body's
    usage block, not a fabricated estimate."""
    resp = client.post("/otlp/v1/logs", json=_claude_code_logs_payload(), headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["accepted"]["claude_code"] == 2

    listing = client.get("/api/sessions", headers=_auth()).json()
    assert len(listing) == 1
    assert listing[0]["session_id"] == "e2e-cc-1"
    assert listing[0]["source"] == "claude_code"
    assert listing[0]["status"] == "open"

    detail = client.get("/api/sessions/e2e-cc-1", headers=_auth()).json()
    assert detail["metrics"]["session"]["source"] == "claude_code"
    assert detail["metrics"]["prompt_metrics"]["input_tokens"] == 150
    assert detail["metrics"]["prompt_metrics"]["output_tokens"] == 40

    timeline = client.get("/api/context-timeline/e2e-cc-1", headers=_auth()).json()
    categories = [b["category"] for b in timeline]
    assert "system" in categories
    assert "answer" in categories


def test_copilot_otlp_traces_flow_to_dashboard_api(client):
    """POST /otlp/v1/traces -> copilot mapper -> store -> /api/sessions
    reflects the session with source='copilot' and real span-derived
    token counts."""
    resp = client.post("/otlp/v1/traces", json=_copilot_traces_payload(), headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["accepted"]["copilot"] == 2

    listing = client.get("/api/sessions", headers=_auth()).json()
    assert len(listing) == 1
    assert listing[0]["source"] == "copilot"

    detail = client.get(f"/api/sessions/{listing[0]['session_id']}", headers=_auth()).json()
    assert detail["metrics"]["prompt_metrics"]["input_tokens"] == 80
    assert detail["metrics"]["prompt_metrics"]["output_tokens"] == 20


def test_three_sources_coexist_in_one_dashboard_view(client):
    """A Bedrock agent session (via /api/record-session), a Claude Code
    session (via OTLP logs), and a Copilot session (via OTLP traces) —
    all under the same owner token — must all show up together in one
    /api/sessions call, each with its own correct source, and the
    dashboard-facing detail route must work identically for all three
    (no source-specific code path leaking through, per the plan's
    'confirm the dashboard code is fully combined' requirement)."""
    bedrock_resp = client.post(
        "/api/record-session",
        json={
            "prompt": "why is x degraded?",
            "model_id": "us.anthropic.claude-sonnet-4-6",
            "loop_result": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "latency_ms": 500,
                "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
                "turns": [{"input_tokens": 100, "output_tokens": 20, "latency_ms": 500}],
            },
        },
        headers=_auth(),
    )
    assert bedrock_resp.status_code == 200
    bedrock_id = bedrock_resp.json()["session_id"]

    client.post("/otlp/v1/logs", json=_claude_code_logs_payload(), headers=_auth())
    client.post("/otlp/v1/traces", json=_copilot_traces_payload(), headers=_auth())

    listing = client.get("/api/sessions?limit=50", headers=_auth()).json()
    assert len(listing) == 3
    sources = {s["source"] for s in listing}
    assert sources == {"bedrock_agent", "claude_code", "copilot"}

    for row in listing:
        detail_resp = client.get(f"/api/sessions/{row['session_id']}", headers=_auth())
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert "metrics" in detail and "turns" in detail and "trace" in detail
        assert detail["metrics"]["session"]["source"] == row["source"]

    # Sanity: the pre-existing bedrock session is still exactly as
    # record_session left it — OTLP ingestion for other sessions must
    # not have mutated it.
    bedrock_detail = client.get(f"/api/sessions/{bedrock_id}", headers=_auth()).json()
    assert bedrock_detail["metrics"]["prompt_metrics"]["total_tokens"] == 120


def test_otlp_sessions_are_owner_scoped_like_everything_else(client, isolated_auth_store):
    """A per-user token must only see its own OTLP-ingested sessions —
    same owner-isolation contract as the rest of the dashboard, proven
    end to end through the real auth middleware, not just at the store
    layer (already covered in tests/test_metrics_store_sqlite.py)."""
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    client.post("/otlp/v1/logs", json=_claude_code_logs_payload("alice-session"), headers=_auth(alice_token))
    client.post("/otlp/v1/logs", json=_claude_code_logs_payload("bob-session"), headers=_auth(bob_token))

    alice_view = client.get("/api/sessions", headers=_auth(alice_token)).json()
    bob_view = client.get("/api/sessions", headers=_auth(bob_token)).json()
    assert [s["session_id"] for s in alice_view] == ["alice-session"]
    assert [s["session_id"] for s in bob_view] == ["bob-session"]

    # Alice can't read Bob's session by ID either — same "not found," not
    # a 403, so this can't be used to probe which session_ids exist.
    cross_read = client.get("/api/sessions/bob-session", headers=_auth(alice_token))
    assert cross_read.status_code == 404


def test_malformed_and_unrecognized_otlp_payloads_do_not_crash_the_pipeline(client):
    """A batch with an unrecognized vendor (no matching service.name/
    gen_ai.*/session.id signal) must be counted as skipped, not crash
    the route or silently corrupt other sessions in the same request."""
    resp = client.post(
        "/otlp/v1/logs",
        json={
            "resourceLogs": [
                {
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "some-other-tool"}}]},
                    "scopeLogs": [{"logRecords": [{"attributes": [], "body": {"stringValue": "irrelevant"}}]}],
                }
            ]
        },
        headers=_auth(),
    )
    assert resp.status_code == 200
    assert resp.json()["accepted"]["skipped"] == 1
    assert client.get("/api/sessions", headers=_auth()).json() == []


def test_dashboard_page_renders_after_real_otlp_ingestion(client, monkeypatch):
    """The /auth/login page (which embeds the dashboard JS/markup) must
    still render successfully once real OTLP-sourced data exists —
    proves the dashboard rebuild's markup and the ingestion pipeline
    aren't accidentally coupled in a way that breaks page rendering."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    client.post("/otlp/v1/logs", json=_claude_code_logs_payload(), headers=_auth())
    resp = client.get("/auth/login")
    assert resp.status_code == 200
    assert "kpi-strip" in resp.text
    assert "body-grid" in resp.text


def test_otlp_cannot_hijack_another_owners_session(client, isolated_auth_store):
    """A per-user token must not be able to write into a session_id that
    already belongs to a different owner, even though OTLP session_ids
    are client-chosen (not server-generated uuid4s) — regression test
    for a cross-tenant write-authorization bypass found in review."""
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    resp = client.post("/otlp/v1/logs", json=_claude_code_logs_payload("shared-id"), headers=_auth(alice_token))
    assert resp.status_code == 200
    assert resp.json()["accepted"]["claude_code"] == 2

    # Bob attempts to write into Alice's existing session_id.
    resp2 = client.post("/otlp/v1/logs", json=_claude_code_logs_payload("shared-id"), headers=_auth(bob_token))
    assert resp2.status_code == 200  # batch succeeds, but the colliding record is silently skipped

    # Alice's session is untouched — still exactly what she wrote, no
    # data injected by Bob, and Bob sees no session at all.
    alice_view = client.get("/api/sessions", headers=_auth(alice_token)).json()
    bob_view = client.get("/api/sessions", headers=_auth(bob_token)).json()
    assert len(alice_view) == 1
    assert alice_view[0]["session_id"] == "shared-id"
    assert bob_view == []
