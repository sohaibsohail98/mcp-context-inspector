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


# --- Per-device / per-session tokens ---------------------------------

CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
FIREFOX_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
CLI_UA = "claude-code/1.2.3"


def test_device_token_mints_and_validates_with_a_label(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    assert store.is_valid_token(token)
    assert store.get_sub_for_token(token) == "sub123"
    devices = store.list_tokens("sub123")
    assert len(devices) == 1
    assert devices[0]["label"] == "Chrome on macOS"
    assert devices[0]["token_id"] and token not in devices[0]["token_id"]


def test_device_token_is_idempotent_per_user_agent(isolated_auth_store):
    """Re-signing-in from the same browser returns the SAME token (so a
    config already pasted elsewhere isn't invalidated), a different
    User-Agent gets its own token and its own device-list row."""
    store = isolated_auth_store
    first = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    again = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    other = store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    assert first == again
    assert other != first
    assert len(store.list_tokens("sub123")) == 2


def test_unknown_user_agent_labels_as_unknown_device(isolated_auth_store):
    store = isolated_auth_store
    store.get_or_create_device_token("sub123", "a@example.com", "")
    assert store.list_tokens("sub123")[0]["label"] == "Unknown device"


def test_list_tokens_marks_the_current_token(isolated_auth_store):
    store = isolated_auth_store
    laptop = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    rows = store.list_tokens("sub123", current_token=laptop)
    current = [r for r in rows if r["is_current"]]
    assert len(current) == 1
    assert current[0]["label"] == "Chrome on macOS"


def test_list_tokens_never_returns_a_raw_token(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", CLI_UA)
    for row in store.list_tokens("sub123"):
        assert token not in row.values()
        assert "token" not in row


def test_list_tokens_is_scoped_to_the_caller(isolated_auth_store):
    store = isolated_auth_store
    store.get_or_create_device_token("alice", "alice@example.com", CHROME_MAC)
    store.get_or_create_device_token("bob", "bob@example.com", FIREFOX_WIN)
    assert len(store.list_tokens("alice")) == 1
    assert len(store.list_tokens("bob")) == 1


def test_revoke_token_kills_exactly_one(isolated_auth_store):
    """The core property: revoking one device leaves every other token
    for that account still valid."""
    store = isolated_auth_store
    laptop = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    work = store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    work_id = next(r["token_id"] for r in store.list_tokens("sub123", current_token=work) if r["is_current"])

    store.revoke_token("sub123", work_id)

    assert store.is_valid_token(work) is False
    assert store.is_valid_token(laptop) is True
    assert len(store.list_tokens("sub123")) == 1


def test_revoke_token_cannot_touch_another_users_token(isolated_auth_store):
    store = isolated_auth_store
    alice_laptop = store.get_or_create_device_token("alice", "alice@example.com", CHROME_MAC)
    bob_laptop = store.get_or_create_device_token("bob", "bob@example.com", FIREFOX_WIN)
    bob_id = store.list_tokens("bob")[0]["token_id"]

    # Alice passes Bob's token_id; scoped by google_sub, so it matches nothing.
    store.revoke_token("alice", bob_id)

    assert store.is_valid_token(bob_laptop) is True
    assert store.is_valid_token(alice_laptop) is True


def test_revoke_token_is_idempotent(isolated_auth_store):
    store = isolated_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    token_id = store.list_tokens("sub123")[0]["token_id"]
    assert store.revoke_token("sub123", token_id) is True
    assert store.revoke_token("sub123", token_id) is False  # already gone, no error
    assert store.revoke_token("sub123", "never-existed") is False
    assert store.is_valid_token(token) is False


def test_account_wide_revoke_nukes_every_device_and_oauth_token(isolated_auth_store):
    store = isolated_auth_store
    shared = store.get_or_create_token("sub123", "a@example.com")
    laptop = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    work = store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")

    store.revoke("sub123")

    for t in (shared, laptop, work, connector):
        assert store.is_valid_token(t) is False
    assert store.list_tokens("sub123") == []


def test_oauth_token_shows_up_in_list_tokens_as_a_connector(isolated_auth_store):
    store = isolated_auth_store
    store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    rows = store.list_tokens("sub123")
    assert len(rows) == 1
    assert rows[0]["kind"] == "connector"
    assert "claude.ai" in rows[0]["label"]


def test_revoke_token_can_revoke_a_connector_session(isolated_auth_store):
    store = isolated_auth_store
    sign_in = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    connector_id = next(r["token_id"] for r in store.list_tokens("sub123") if r["kind"] == "connector")

    store.revoke_token("sub123", connector_id)

    assert store.is_valid_token(connector) is False
    assert store.is_valid_token(sign_in) is True


def test_touch_token_refreshes_last_seen_at_most_hourly(isolated_auth_store, monkeypatch):
    store = isolated_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    first_seen = store.list_tokens("sub123")[0]["last_seen_at"]

    # A touch within the hour is a no-op.
    store.touch_token(token)
    assert store.list_tokens("sub123")[0]["last_seen_at"] == first_seen

    # Force the stored value to look old, then a touch must move it.
    import time as _t

    monkeypatch.setattr(_t, "time", lambda: first_seen + store.LAST_SEEN_REFRESH_SECONDS + 60)
    store.touch_token(token)
    assert store.list_tokens("sub123")[0]["last_seen_at"] > first_seen


def test_touch_token_on_unknown_token_does_not_raise(isolated_auth_store):
    isolated_auth_store.touch_token("not-a-real-token")


def test_predate_metadata_oauth_token_still_valid_and_listed(isolated_auth_store):
    """Migration: an oauth_tokens row written before the metadata columns
    existed (no token_id/label) must still authenticate and still appear
    in the device list, just as 'Unknown device' / unlabelled."""
    store = isolated_auth_store
    token = store.mint_oauth_token("sub123", "a@example.com", "old-client")
    # Simulate the pre-migration shape by blanking the new columns.
    conn = store._connect()
    conn.execute("UPDATE oauth_tokens SET token_id=NULL, label=NULL, last_seen_at=NULL WHERE token=?", (token,))
    conn.commit()
    conn.close()

    assert store.is_valid_token(token) is True
    rows = store.list_tokens("sub123")
    assert len(rows) == 1
    assert rows[0]["token_id"] is None
    assert rows[0]["label"]  # falls back to a client-name label, never blank
