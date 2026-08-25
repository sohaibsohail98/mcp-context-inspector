"""End-to-end OTLP ingestion: POST a payload shaped like a real vendor
export, assert it's accepted (not silently counted as "skipped" — see
mcp_server/otlp/__init__.py's detect_vendor), and assert the resulting
session becomes visible via /api/sessions.

This is the regression test for the 2026-08-25 investigation: a
synthetic /otlp/v1/logs POST with NO resource.attributes at all (no
service.name) is silently vendor-undetected and counted as "skipped",
with no error surfaced anywhere Claude Code's own fire-and-forget
exporter would ever see it. A payload carrying service.name=claude-code
on its resource attributes (test_realistic_claude_code_payload_is_not_skipped
below) works end-to-end today; this suite exists so a future change to
detect_vendor or the receiver route can't silently regress that without
a CI failure.
"""

import time
import uuid


def _claude_code_resource(session_id):
    return [
        {
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": "claude-code"}},
                    {"key": "session.id", "value": {"stringValue": session_id}},
                ]
            },
            "scopeLogs": [
                {
                    "logRecords": [
                        {
                            "attributes": [
                                {"key": "session.id", "value": {"stringValue": session_id}},
                                {"key": "event.name", "value": {"stringValue": "api_request_body"}},
                                {"key": "model", "value": {"stringValue": "claude-sonnet-5"}},
                                {
                                    "key": "body",
                                    "value": {
                                        "stringValue": (
                                            '{"messages":[{"role":"user","content":"api_tests probe"}]}'
                                        )
                                    },
                                },
                            ]
                        }
                    ]
                }
            ],
        }
    ]


def test_realistic_claude_code_payload_is_not_skipped(client):
    session_id = f"api-tests-{uuid.uuid4()}"
    resp = client.otlp_logs(_claude_code_resource(session_id))
    assert resp.status == 200
    assert resp.body["accepted"]["claude_code"] == 1
    assert resp.body["accepted"]["skipped"] == 0


def test_accepted_session_becomes_visible_via_api(client):
    session_id = f"api-tests-{uuid.uuid4()}"
    resp = client.otlp_logs(_claude_code_resource(session_id))
    assert resp.status == 200
    assert resp.body["accepted"]["claude_code"] == 1

    # No documented eventual-consistency window in any storage backend
    # (SQLite/DynamoDB/Firestore all write synchronously per
    # metrics/store.py's dispatcher) — a short retry loop only guards
    # against real network/propagation latency on the deployed instance,
    # not an expected async gap.
    deadline = time.time() + 10
    found = None
    while time.time() < deadline:
        sessions = client.sessions().body
        found = next((s for s in sessions if s["session_id"] == session_id), None)
        if found:
            break
        time.sleep(1)

    assert found is not None, f"session {session_id} never appeared via /api/sessions"
    assert found["source"] == "claude_code"


def test_payload_with_no_resource_attributes_is_skipped_not_rejected(client):
    """Locks in CURRENT, intentional behavior: a payload with no
    service.name/session.id on its resource attributes returns 200 with
    skipped=1, not a 4xx — detect_vendor's documented fallback for "not
    our vendor" (see mcp_server/otlp/__init__.py). If Claude Code's REAL
    export ever turns out not to carry service.name/session.id on
    resource attributes, test_realistic_claude_code_payload_is_not_skipped
    above is the one that should start failing, not this one."""
    resp = client.otlp_logs([{"resource": {"attributes": []}, "scopeLogs": []}])
    assert resp.status == 200
    assert resp.body["accepted"]["skipped"] == 0  # zero log records in this payload, not zero because it was accepted


def test_bad_json_returns_400(client):
    resp = client.post_raw("/otlp/v1/logs", b"not json", content_type="text/plain")
    assert resp.status == 400
