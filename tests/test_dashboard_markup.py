"""Regression test for the rebuilt live-dashboard markup on /auth/login
(replacing the simple session-list/detail rendering from commit ec74e7b
with the mockup-matching layout: KPI strip, quota strip, body-grid with
session-list + tabbed session-detail panels, and the settings screen).

Like test_connect_page_otel_tabs.py, the dashboard is built by
client-side JS (mountDashboard/renderDashboardShell/etc.), but its
source — including the template-literal HTML fragments it emits — is
rendered as literal text in the server-side /auth/login response, so
these structural class/attribute markers are present in the HTTP
response body without needing to execute any JS.
"""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def test_dashboard_markup_has_new_structural_markers(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    assert resp.status_code == 200
    body = resp.text

    for marker in (
        'class="kpi-strip"',
        'class="body-grid"',
        'data-tab="overview"',
        'data-tab="context"',
        'data-tab="tools"',
        'data-tab="breakdown"',
        'class="quota-card pending"',
        'class="settings-wrap"',
    ):
        assert marker in body, f"missing marker: {marker}"


def test_dashboard_omits_fabricated_insight_list(monkeypatch):
    """The 30-type insight-card backlog is explicitly deferred past v1
    (docs/OTLP_INTEGRATION_PLAN.md) — the rebuild must not render an
    .insight-list section or fabricated example insights."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    body = resp.text
    assert "insight-list" not in body


def test_quota_strip_has_no_fabricated_percentage(monkeypatch):
    """The 5h/7d usage-window quota cards are not wired to any real data
    source yet — must render as pending, not a fabricated static number
    like the mockup's own example percentages."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    body = resp.text
    assert "pending data source" in body
    assert "Not yet wired to a data source" in body


def test_settings_toggle_reuses_existing_tab_pattern(monkeypatch):
    """The ⚙ settings toggle must exist and follow the same show/hide-
    sibling-divs pattern already used for showConnectTab, not a new
    screen-router — per the task's explicit "reuse that pattern" note."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")

    body = resp.text
    assert "toggleSettings()" in body
    assert "function toggleSettings()" in body
    assert 'id="dash-root"' in body
    assert 'id="settings-root"' in body
