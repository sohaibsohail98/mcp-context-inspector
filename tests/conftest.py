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
    """Point auth_store at a fresh, empty DB file per test — same
    reasoning as isolated_sqlite_db, never the developer's real
    data/mcp_auth.db (real per-user tokens)."""
    from mcp_server import auth_store

    monkeypatch.setattr(auth_store, "DB_PATH", tmp_path / "test_mcp_auth.db")
    return auth_store
