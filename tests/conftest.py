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
