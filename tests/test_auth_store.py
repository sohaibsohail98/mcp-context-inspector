"""Regression tests for mcp_server/auth_store.py, the per-user MCP
token store backing the Google sign-in flow.
"""

import threading


def test_get_or_create_token_mints_a_new_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_token("sub123", "a@example.com")
    assert token
    assert store.is_valid_token(token)


def test_get_or_create_token_is_idempotent_per_google_sub(isolated_auth_store):
    """The actual property this exists for: signing in twice must return
    the SAME token, not silently mint (and orphan) a second one. A
    friend who re-runs /auth/login shouldn't have their already-pasted
    MCP client config invalidated."""
    store = isolated_auth_store
    first = store.get_or_create_token("sub123", "a@example.com")
    second = store.get_or_create_token("sub123", "a@example.com")
    assert first == second


def test_get_or_create_token_updates_email_without_changing_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_token("sub123", "old@example.com")
    token_again = store.get_or_create_token("sub123", "new@example.com")
    assert token == token_again
    users = store.list_users()
    assert users[0]["email"] == "new@example.com"


def test_different_google_accounts_get_different_tokens(isolated_auth_store):
    store = isolated_auth_store
    token_a = store.get_or_create_token("sub-a", "a@example.com")
    token_b = store.get_or_create_token("sub-b", "b@example.com")
    assert token_a != token_b


def test_is_valid_token_rejects_unknown_token(isolated_auth_store):
    store = isolated_auth_store
    assert store.is_valid_token("not-a-real-token") is False


def test_is_valid_token_on_empty_db_does_not_crash(isolated_auth_store):
    store = isolated_auth_store
    assert store.is_valid_token("anything") is False


def test_revoke_invalidates_the_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_token("sub123", "a@example.com")
    assert store.is_valid_token(token)
    store.revoke("sub123")
    assert store.is_valid_token(token) is False


def test_revoke_unknown_sub_does_not_crash(isolated_auth_store):
    store = isolated_auth_store
    store.revoke("nonexistent-sub")  # must not raise


def test_list_users_never_includes_tokens(isolated_auth_store):
    store = isolated_auth_store
    store.get_or_create_token("sub123", "a@example.com")
    users = store.list_users()
    assert len(users) == 1
    assert "token" not in users[0]
    assert users[0]["google_sub"] == "sub123"
    assert users[0]["email"] == "a@example.com"


def test_concurrent_first_sign_in_for_the_same_account_never_crashes(isolated_auth_store):
    """N threads racing to be the FIRST sign-in for a brand-new
    google_sub, e.g. a friend double-clicking "Sign in with Google," or
    two server worker processes handling near-simultaneous requests.
    Every thread must both succeed AND agree on exactly one winning
    token, with no crash and no silently-orphaned second token."""
    store = isolated_auth_store
    barrier = threading.Barrier(20)
    results = []
    errors = []
    lock = threading.Lock()

    def worker():
        barrier.wait()  # force maximum contention on the same instant
        try:
            token = store.get_or_create_token("contested-sub", "a@example.com")
            with lock:
                results.append(token)
        except Exception as e:  # noqa: BLE001 (the test itself is the assertion)
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(results) == 20
    assert len(set(results)) == 1  # every thread agrees on the one real token
    assert len(store.list_users()) == 1  # no duplicate/orphaned rows


def test_list_oauth_clients_returns_registered_clients(isolated_auth_store):
    store = isolated_auth_store
    client_id = store.register_oauth_client(["https://example.com/callback"], client_name="Test Client")
    clients = store.list_oauth_clients()
    assert len(clients) == 1
    assert clients[0]["client_id"] == client_id
    assert clients[0]["client_name"] == "Test Client"
    assert clients[0]["redirect_uris"] == ["https://example.com/callback"]


def test_revoke_oauth_client_removes_the_registration(isolated_auth_store):
    store = isolated_auth_store
    client_id = store.register_oauth_client(["https://example.com/callback"])
    store.revoke_oauth_client(client_id)
    assert store.list_oauth_clients() == []
    assert store.get_oauth_client(client_id) is None


def test_revoke_oauth_client_invalidates_its_unconsumed_codes(isolated_auth_store):
    store = isolated_auth_store
    client_id = store.register_oauth_client(["https://example.com/callback"])
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/callback", "challenge", "https://example.com/mcp"
    )
    store.revoke_oauth_client(client_id)
    import pytest

    with pytest.raises(ValueError):
        store.redeem_oauth_code(code, client_id, "https://example.com/callback", "verifier", "https://example.com/mcp")


def test_revoke_oauth_client_unknown_id_does_not_crash(isolated_auth_store):
    store = isolated_auth_store
    store.revoke_oauth_client("nonexistent-client")  # must not raise


def test_list_oauth_tokens_never_includes_the_raw_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    tokens = store.list_oauth_tokens()
    assert len(tokens) == 1
    assert tokens[0]["google_sub"] == "sub123"
    assert tokens[0]["client_name"] == "claude.ai"
    assert "token" not in tokens[0]
    assert token not in tokens[0]["token_prefix"]


def test_revoke_oauth_token_invalidates_it(isolated_auth_store):
    store = isolated_auth_store
    token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    assert store.is_valid_token(token)
    store.revoke_oauth_token(token)
    assert store.is_valid_token(token) is False


def test_revoke_oauth_token_does_not_touch_the_users_own_sign_in_token(isolated_auth_store):
    """The whole point of a separate oauth_tokens table: disconnecting one
    OAuth client must never break the same user's direct MCP config."""
    store = isolated_auth_store
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    store.revoke_oauth_token(oauth_token)
    assert store.is_valid_token(sign_in_token) is True


def test_revoke_oauth_token_unknown_token_does_not_crash(isolated_auth_store):
    store = isolated_auth_store
    store.revoke_oauth_token("not-a-real-token")  # must not raise
