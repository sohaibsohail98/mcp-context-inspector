"""Regression test for the rebuilt live-dashboard markup on /auth/login
(replacing the simple session-list/detail rendering from commit ec74e7b
with the mockup-matching layout: KPI strip, quota strip, body-grid with
session-list + tabbed session-detail panels, and the settings screen).

The dashboard is built by client-side JS (mountDashboard/
renderDashboardShell/etc.). That JS, including the template-literal HTML
fragments it emits, used to be inlined in the /auth/login response; it
now lives in the linked static file dashboard/dashboard.js (served at
/auth/static/dashboard.js). `_page_text()` stitches the login HTML
together with its linked JS/CSS so these structural class/attribute and
function markers can be asserted on exactly as before, wherever they now
live.
"""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def _page_text(client):
    """The /auth/login HTML plus every same-origin script/style it links,
    concatenated: the full set of source the browser gets for that page."""
    html = client.get("/auth/login").text
    js = client.get("/auth/static/dashboard.js").text
    css = client.get("/auth/static/dashboard.css").text
    return "\n".join([html, js, css])


def test_dashboard_markup_has_new_structural_markers(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        assert client.get("/auth/login").status_code == 200
        body = _page_text(client)

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
    The rebuild must not render an
    .insight-list section or fabricated example insights."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    assert "insight-list" not in body


def test_quota_strip_has_no_fabricated_percentage(monkeypatch):
    """The 5h/7d usage-window quota cards are not wired to any real data
    source yet, so it must render as pending, not a fabricated static number
    like the mockup's own example percentages."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    assert "pending data source" in body
    assert "Not yet wired to a data source" in body


def test_refresh_controls_present(monkeypatch):
    """The Sessions panel must offer a manual refresh button and an
    auto-refresh toggle, reusing the existing refreshDashboard/dashboardTimer
    machinery rather than a second competing fetch/polling mechanism."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    for marker in (
        'class="refresh-controls"',
        'id="manual-refresh-btn"',
        "function manualRefresh()",
        "function toggleAutoRefresh()",
        "let dashboardAutoRefresh",
    ):
        assert marker in body, f"missing marker: {marker}"


def test_auto_refresh_is_throttled_and_pauses_when_tab_hidden(monkeypatch):
    """Pin the throttle: a >=30s poll interval constant, a document.hidden
    guard on the automatic poll path, and a session limit well under the
    old 500."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    assert "const DASHBOARD_POLL_MS = " in body
    # Extract the interval and assert it's not the old sub-10s cadence.
    import re

    poll_ms = int(re.search(r"const DASHBOARD_POLL_MS = (\d+)", body).group(1))
    assert poll_ms >= 30000, f"poll interval {poll_ms}ms is too aggressive"

    assert "document.hidden" in body, "automatic poll must skip while tab is hidden"
    assert "visibilitychange" in body, "must refresh once when tab returns to foreground"

    limit = int(re.search(r"const DASHBOARD_SESSION_LIMIT = (\d+)", body).group(1))
    assert limit <= 200, f"session limit {limit} too high"
    assert "limit=500" not in body, "old hardcoded limit=500 still present"
    assert "setInterval(() => refreshDashboard(token), 8000)" not in body


def test_settings_toggle_reuses_existing_tab_pattern(monkeypatch):
    """The ⚙ settings toggle must exist and follow the same show/hide-
    sibling-divs pattern already used for showConnectTab, not a new
    screen-router, per the task's explicit "reuse that pattern" note."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    assert "toggleSettings()" in body
    assert "function toggleSettings()" in body
    assert 'id="dash-root"' in body
    assert 'id="settings-root"' in body


def test_context_block_label_is_escaped_before_innerhtml(monkeypatch):
    """renderContextBlockRow() puts b.label/b.category into innerHTML.
    Both come from record_session/append_context_block, caller-supplied
    data from any signed-in user; the owner token can see every user's
    sessions (see _visible() in metrics/store_sqlite.py), so an
    unescaped label is a stored XSS that fires in the owner's browser,
    where their bearer token sits in localStorage. Guard against a
    regression back to the raw '+ (b.label || b.category) +' form."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        body = _page_text(client)

    assert "escapeHtml(b.label || b.category)" in body
    assert "(b.label || b.category) + '</span>'" not in body
    assert "': (b.label || b.category)" not in body
