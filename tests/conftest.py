"""Shared pytest fixtures. These tests are pure unit tests: no live
Bedrock/AWS calls.
"""

import pytest


@pytest.fixture
def isolated_sqlite_db(tmp_path, monkeypatch):
    """Point store_sqlite at a fresh, empty DB file per test, never the
    real data/metrics.db, so tests can't see each other's data or the
    developer's real local history."""
    from metrics import store_sqlite

    monkeypatch.setattr(store_sqlite, "DB_PATH", tmp_path / "test_metrics.db")
    return store_sqlite


@pytest.fixture
def isolated_firestore_db(monkeypatch):
    """Point store_firestore at a fresh, unique top-level collection per
    test. Same reasoning as isolated_sqlite_db, but Firestore (even the
    emulator) has no per-test DB-file-reset primitive like SQLite's
    tmp_path, so isolation instead comes from a unique collection name
    per test rather than a unique database. Requires the emulator (see
    FIRESTORE_EMULATOR_HOST); callers are responsible for skipping when
    it isn't reachable."""
    import uuid

    from metrics import store_firestore

    monkeypatch.setattr(store_firestore, "SESSIONS_COLLECTION", f"test_sessions_{uuid.uuid4().hex}")
    return store_firestore


@pytest.fixture
def isolated_auth_store(tmp_path, monkeypatch):
    """Point the SQLite auth backend at a fresh, empty DB file per test,
    same reasoning as isolated_sqlite_db, never the developer's real
    data/mcp_auth.db (real per-user tokens). Imports auth_store_sqlite
    directly (not the auth_store dispatcher) for the same reason
    isolated_sqlite_db imports store_sqlite directly: DB_PATH lives on
    the concrete backend module, and functions reached only via the
    dispatcher's re-exported names would still read the *backend's own*
    module-global DB_PATH, not a copy on the dispatcher. Patching the
    dispatcher's attribute wouldn't reach them."""
    from mcp_server.auth import store_sqlite as auth_store_sqlite

    monkeypatch.setattr(auth_store_sqlite, "DB_PATH", tmp_path / "test_mcp_auth.db")
    return auth_store_sqlite


@pytest.fixture
def isolated_firestore_auth_store():
    """Point the Firestore auth backend at a fresh set of collections per
    test, via a unique collection-name prefix. Same isolation goal as
    isolated_auth_store, but Firestore (even the emulator) has no
    per-test "fresh DB file" equivalent, so prefixing collection names
    is the practical substitute: each test gets its own
    mcp_users/oauth_clients/oauth_codes/oauth_tokens namespace and tests
    can't see each other's data. Requires FIRESTORE_EMULATOR_HOST to be
    set; callers are expected to skip when it isn't (see
    tests/test_auth_store_firestore.py's module-level skipif)."""
    import uuid

    from mcp_server.auth import store_firestore as auth_store_firestore

    prefix = f"test{uuid.uuid4().hex[:8]}_"
    original_collections = {
        name: getattr(auth_store_firestore, name)
        for name in ("_users", "_clients", "_codes", "_tokens")
    }

    def _mk(collection_name):
        def _accessor():
            return auth_store_firestore._db().collection(prefix + collection_name)

        return _accessor

    auth_store_firestore._users = _mk("mcp_users")
    auth_store_firestore._clients = _mk("oauth_clients")
    auth_store_firestore._codes = _mk("oauth_codes")
    auth_store_firestore._tokens = _mk("oauth_tokens")
    try:
        yield auth_store_firestore
    finally:
        for name, fn in original_collections.items():
            setattr(auth_store_firestore, name, fn)


@pytest.fixture(autouse=True)
def _reset_oauth_register_rate_limit():
    """routes_oauth._register_attempts is module-level, in-memory state
    (see that module's docstring on why). Without this, tests that hit
    POST /oauth/register more than _REGISTER_MAX_PER_WINDOW times across
    the whole test session would start getting real 429s from each
    other, not from anything the test itself is checking. Autouse so
    every test starts with a clean window regardless of who ran before
    it."""
    from mcp_server.routes import oauth as routes_oauth

    routes_oauth._register_attempts.clear()
    yield
    routes_oauth._register_attempts.clear()


@pytest.fixture(autouse=True)
def _reset_otlp_debug_counters():
    """mcp_server.otlp's _counts/_last_accepted_at/_recent_skipped are
    module-level, in-memory state (see that module's docstring). Same
    reasoning as _reset_oauth_register_rate_limit above: without this,
    GET /otlp/debug tests would see counters left over from whatever
    other test sent OTLP payloads earlier in the same test session,
    across the same owner keys."""
    from mcp_server import otlp

    otlp._counts.clear()
    otlp._last_accepted_at.clear()
    otlp._recent_skipped.clear()
    yield
    otlp._counts.clear()
    otlp._last_accepted_at.clear()
    otlp._recent_skipped.clear()
