"""Regression test for the production 421 "Invalid Host header" bug
(fixed in commit e73d299): mcp_server/server.py's `__main__` block builds
`server.streamable_http_app()` with an explicit `TransportSecuritySettings`
whose `allowed_hosts` includes `127.0.0.1:*`/`localhost:*`/`[::1]:*` plus
whatever `MCP_ALLOWED_HOSTS` adds, e.g. the real Cloud Run hostname the
Cloudflare Worker proxy forwards as the Host header.

Before that fix, `streamable_http_app()` was called bare (no
`transport_security` argument), which the MCP SDK's lowlevel Server
silently interprets as "auto-enable DNS-rebinding protection scoped to
127.0.0.1 only", so a real deployed request, arriving with the Cloud
Run service's own hostname in its Host header, always got rejected with
421, even with a perfectly valid bearer token.

`test_mcp_protocol_ownership.py` and `test_server_auth.py`'s fixtures
both call `server.streamable_http_app()` bare too, deliberately, so
`TestClient`'s own Host header ("testserver") isn't rejected by
DNS-rebinding protection they aren't trying to exercise. That means
neither file would have caught this bug: it never builds transport
security the way production does. This file does exactly that
production construction and sends requests with actual Host headers,
so a regression here fails a test instead of only showing up as a live
421.
"""

import pytest
from starlette.testclient import TestClient

from mcp.server.transport_security import TransportSecuritySettings

from mcp_server import server as server_module

_PROD_CLOUD_RUN_HOST = "sre-ctxwindow-abc123-uc.a.run.app"
_UNLISTED_HOST = "evil.attacker.example"


def _build_prod_style_app(allowed_hosts):
    """Mirrors mcp_server/server.py's `__main__` block: an explicit
    TransportSecuritySettings built from the base loopback hosts plus
    whatever MCP_ALLOWED_HOSTS (here, passed directly) adds, not the
    bare `streamable_http_app()` call the other test files use."""
    app = server_module.server.streamable_http_app(
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*", *allowed_hosts],
            allowed_origins=["http://127.0.0.1:8788", "http://localhost:8788"],
        )
    )
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    return app


@pytest.fixture
def prod_style_client(isolated_auth_store, isolated_sqlite_db):
    app = _build_prod_style_app([_PROD_CLOUD_RUN_HOST])
    with TestClient(app, base_url=f"http://{_PROD_CLOUD_RUN_HOST}") as c:
        yield c


def _initialize_body():
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "pytest-prod-style-client", "version": "1.0.0"},
        },
    }


def test_allowlisted_production_host_is_accepted_not_421(prod_style_client):
    """The actual bug: a real production Host header (the Cloud Run
    hostname, added via what MCP_ALLOWED_HOSTS supplies in real
    deployment) must be accepted once explicitly allowlisted."""
    resp = prod_style_client.post(
        "/mcp",
        json=_initialize_body(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer owner-secret",
        },
    )
    assert resp.status_code != 421, resp.text
    assert resp.status_code < 400, resp.text


def test_unlisted_host_is_still_rejected_with_421(isolated_auth_store, isolated_sqlite_db):
    """Proves the allowlist is real, not effectively disabled: a Host
    header that was never added to allowed_hosts must still be
    rejected. DNS-rebinding protection stays on, just correctly
    scoped."""
    app = _build_prod_style_app([_PROD_CLOUD_RUN_HOST])
    with TestClient(app, base_url=f"http://{_UNLISTED_HOST}") as c:
        resp = c.post(
            "/mcp",
            json=_initialize_body(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer owner-secret",
            },
        )
    assert resp.status_code == 421


def test_bare_streamable_http_app_would_have_rejected_the_prod_host(isolated_auth_store, isolated_sqlite_db):
    """Documents the bug's actual mechanism: without an explicit
    transport_security argument (the pre-fix code path), even the
    legitimate production Host header gets a 421, because the SDK's
    default DNS-rebinding protection only ever allows 127.0.0.1."""
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    with TestClient(app, base_url=f"http://{_PROD_CLOUD_RUN_HOST}") as c:
        resp = c.post(
            "/mcp",
            json=_initialize_body(),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": "Bearer owner-secret",
            },
        )
    assert resp.status_code == 421
