"""Smoke tests for the mobile session-history webapp's static routes
(mcp_server/routes/webapp.py) — the /m shell, its static assets, and
the redirect-handoff addition in routes/auth.py's /auth/login. Mirrors
the TestClient-against-the-real-app pattern used by
test_dashboard_markup.py / test_connect_page_otel_tabs.py rather than
inventing a new harness.
"""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def _client():
    app = server_module.server.streamable_http_app()
    return TestClient(app)


def test_webapp_root_redirects_to_trailing_slash():
    with _client() as client:
        resp = client.get("/m", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/m/"


def test_webapp_index_serves_shell_html():
    with _client() as client:
        resp = client.get("/m/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "mcp-context-inspector" in resp.text
    assert 'id="app"' in resp.text
    assert '/m/app.js' in resp.text
    assert '/m/styles.css' in resp.text


def test_webapp_serves_app_js():
    with _client() as client:
        resp = client.get("/m/app.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "mciw_token" in resp.text


def test_webapp_serves_styles_css():
    with _client() as client:
        resp = client.get("/m/styles.css")
    assert resp.status_code == 200
    assert "css" in resp.headers["content-type"]
    assert "--accent: #6cbfa4" in resp.text


def test_webapp_unknown_asset_redirects_to_index():
    with _client() as client:
        resp = client.get("/m/does-not-exist.js", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/m/"


def test_webapp_routes_are_not_gated_by_bearer_auth():
    """/m/* serves static assets only (no per-user data) — auth happens
    client-side in app.js against /api/*, which stays protected."""
    with _client() as client:
        resp = client.get("/m/")
    assert resp.status_code == 200


def test_auth_login_return_to_handoff_present(monkeypatch):
    """The minimal, isolated addition to /auth/login's post-sign-in JS:
    when reached via ?return_to=/m, the authorize() success branch must
    redirect back there with the token in the URL fragment instead of
    rendering the desktop consent/dashboard screens."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    with _client() as client:
        resp = client.get("/auth/login")

    assert resp.status_code == 200
    body = resp.text
    assert "return_to" in body
    assert "#token=" in body
