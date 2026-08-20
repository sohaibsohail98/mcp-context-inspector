"""Regression test for the OTLP onboarding tabs added to connectPage()'s
embedded JS in /auth/login's HTML response. connectPage() itself is a
client-side JS function (not Python), but its source — including the
template-literal HTML it builds — is rendered as literal text in the
server-side /auth/login response, so the markers below are present in
the HTTP response body without needing to execute any JS.
"""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def test_connect_page_has_otel_tabs_with_separate_optin_blocks(monkeypatch):
    """Both new telemetry tabs must exist, and each one's raw-content
    opt-in line (OTEL_LOG_RAW_API_BODIES / COPILOT_OTEL_CAPTURE_CONTENT)
    must live in a visually distinct block from the base snippet — that
    separation is the whole point (bigger disclosure, opt-in, not
    bundled silently into what a user copies by default)."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    assert resp.status_code == 200
    body = resp.text

    assert 'data-tab="claude-otel"' in body
    assert 'data-tab="copilot-otel"' in body
    assert "CLAUDE_CODE_ENABLE_TELEMETRY" in body
    assert "COPILOT_OTEL_ENABLED" in body

    # The base Claude Code OTel snippet must NOT itself contain the
    # raw-body opt-in line — it lives in its own separate <pre>/block.
    claude_snippet_start = body.index('id="claude-otel-snippet"')
    claude_snippet_end = body.index("</pre>", claude_snippet_start)
    assert "OTEL_LOG_RAW_API_BODIES" not in body[claude_snippet_start:claude_snippet_end]
    assert 'id="claude-otel-optin"' in body

    copilot_snippet_start = body.index('id="copilot-otel-snippet"')
    copilot_snippet_end = body.index("</pre>", copilot_snippet_start)
    assert "OTEL_CAPTURE_CONTENT" not in body[copilot_snippet_start:copilot_snippet_end]
    assert 'id="copilot-otel-optin"' in body


def test_claude_tab_still_default_active(monkeypatch):
    """Existing default-active tab behavior must be unchanged by adding
    the two new tabs after it."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    body = resp.text
    claude_btn_start = body.index('data-tab="claude"')
    claude_btn_line = body[max(0, claude_btn_start - 40):claude_btn_start]
    assert "active" in claude_btn_line
