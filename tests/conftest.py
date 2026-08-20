"""Shared pytest fixtures. These tests are pure unit tests — no live
Bedrock/AWS calls.
"""

import pytest


@pytest.fixture
def isolated_sqlite_db(tmp_path, monkeypatch):
    """Point store_sqlite at a fresh, empty DB file per test — never the
    real data/metrics.db — so tests can't see each other's data or the
    developer's real local history."""
    from metrics import store_sqlite

    monkeypatch.setattr(store_sqlite, "DB_PATH", tmp_path / "test_metrics.db")
    return store_sqlite


@pytest.fixture
def isolated_auth_store(tmp_path, monkeypatch):
    """Point the SQLite auth backend at a fresh, empty DB file per test —
    same reasoning as isolated_sqlite_db, never the developer's real
    data/mcp_auth.db (real per-user tokens). Imports auth_store_sqlite
    directly (not the auth_store dispatcher) for the same reason
    isolated_sqlite_db imports store_sqlite directly: DB_PATH lives on
    the concrete backend module, and functions reached only via the
    dispatcher's re-exported names would still read the *backend's own*
    module-global DB_PATH, not a copy on the dispatcher — patching the
    dispatcher's attribute wouldn't reach them."""
    from mcp_server.auth import store_sqlite as auth_store_sqlite

    monkeypatch.setattr(auth_store_sqlite, "DB_PATH", tmp_path / "test_mcp_auth.db")
    return auth_store_sqlite


@pytest.fixture(autouse=True)
def _reset_oauth_register_rate_limit():
    """routes_oauth._register_attempts is module-level, in-memory state
    (see that module's docstring on why) — without this, tests that hit
    POST /oauth/register more than _REGISTER_MAX_PER_WINDOW times across
    the whole test session would start getting real 429s from each
    other, not from anything the test itself is checking. Autouse so
    every test starts with a clean window regardless of who ran before
    it."""
    from mcp_server.routes import oauth as routes_oauth

    routes_oauth._register_attempts.clear()
    yield
    routes_oauth._register_attempts.clear()
