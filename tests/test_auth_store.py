"""Regression tests for mcp_server/auth_store.py — the per-user MCP
token store backing the Google sign-in flow.
"""


def test_get_or_create_token_mints_a_new_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_token("sub123", "a@example.com")
    assert token
    assert store.is_valid_token(token)


def test_get_or_create_token_is_idempotent_per_google_sub(isolated_auth_store):
    """The actual property this exists for: signing in twice must return
    the SAME token, not silently mint (and orphan) a second one — a
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
