"""Smoke tests for the /docs static site (mcp_server/routes/docs.py):
mirrors test_webapp_routes.py's pattern for the same class of route
(static file, not gated by bearer auth)."""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def _client():
    app = server_module.server.streamable_http_app()
    return TestClient(app)


def test_docs_root_redirects_to_trailing_slash():
    with _client() as client:
        resp = client.get("/docs", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/docs/"


def test_docs_index_serves_html():
    with _client() as client:
        resp = client.get("/docs/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "CtxWindow" in resp.text
    assert "get_session_metrics" in resp.text


def test_docs_route_is_not_gated_by_bearer_auth():
    with _client() as client:
        resp = client.get("/docs/")
    assert resp.status_code == 200
