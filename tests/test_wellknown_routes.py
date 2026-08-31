"""Smoke tests for the site-root static assets (mcp_server/routes/wellknown.py):
robots.txt, sitemap.xml, favicon.{ico,svg}. Same pattern as
test_docs_routes.py -- static files, not gated by bearer auth."""

from starlette.testclient import TestClient

from mcp_server import server as server_module


def _client():
    app = server_module.server.streamable_http_app()
    return TestClient(app)


def test_robots_txt_served_unauthenticated():
    with _client() as client:
        resp = client.get("/robots.txt")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "Sitemap: https://ctxwindow.uk/sitemap.xml" in resp.text
    assert "Disallow: /mcp" in resp.text


def test_sitemap_xml_served_unauthenticated():
    with _client() as client:
        resp = client.get("/sitemap.xml")
    assert resp.status_code == 200
    assert "xml" in resp.headers["content-type"]
    assert "<loc>https://ctxwindow.uk/docs/</loc>" in resp.text


def test_favicon_ico_served():
    with _client() as client:
        resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")
    # real ICO magic: 00 00 01 00
    assert resp.content[:4] == b"\x00\x00\x01\x00"


def test_favicon_svg_served():
    with _client() as client:
        resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert "image/svg+xml" in resp.headers["content-type"]
    assert b"<svg" in resp.content


def test_wellknown_routes_not_gated_by_bearer_auth():
    with _client() as client:
        for path in ("/robots.txt", "/sitemap.xml", "/favicon.ico", "/favicon.svg"):
            assert client.get(path).status_code == 200, path
