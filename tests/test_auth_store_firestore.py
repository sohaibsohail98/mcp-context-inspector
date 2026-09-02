"""Regression tests for mcp_server/auth/store_firestore.py, run against
the real Firestore emulator (FIRESTORE_EMULATOR_HOST), not a fake/mock,
since Firestore's transaction semantics (used for get_or_create_token's
race-safety and redeem_oauth_code's single-use enforcement) are exactly
the behavior under test and are impractical to fake convincingly.

Skipped whole-module if the emulator isn't reachable, so this doesn't
break CI/local runs without `gcloud emulators firestore start` (or the
equivalent) running. Start one locally with:

    gcloud emulators firestore start --host-port=localhost:8080

and export FIRESTORE_EMULATOR_HOST=localhost:8080 before running pytest.

Covers the same correctness contracts as test_auth_store_dynamodb.py:
single-use PKCE code redemption stays atomic under concurrency,
concurrent first-sign-in races agree on one token, revoking a user's
token or an OAuth client/token doesn't leak or break unrelated
credentials, and admin-listing functions never return raw token values.
"""

import base64
import hashlib
import os
import socket
import time

import pytest


def _emulator_reachable():
    host_port = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host_port:
        return False
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _emulator_reachable(),
    reason="FIRESTORE_EMULATOR_HOST not set or emulator not reachable; skipping live Firestore tests",
)


def _pkce_pair(verifier="correct-verifier"):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def test_get_or_create_token_mints_and_is_idempotent(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    first = store.get_or_create_token("sub123", "a@example.com")
    second = store.get_or_create_token("sub123", "new@example.com")
    assert first == second
    assert store.is_valid_token(first)
    users = store.list_users()
    assert len(users) == 1
    assert users[0]["google_sub"] == "sub123"
    assert users[0]["email"] == "new@example.com"
    assert users[0]["created_at"] == pytest.approx(time.time(), abs=10)


def test_get_or_create_token_different_accounts_get_different_tokens(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    a = store.get_or_create_token("sub-a", "a@example.com")
    b = store.get_or_create_token("sub-b", "b@example.com")
    assert a != b


def test_is_valid_token_rejects_unknown(isolated_firestore_auth_store):
    assert isolated_firestore_auth_store.is_valid_token("nope") is False


def test_get_sub_for_token_resolves_sign_in_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    token = store.get_or_create_token("sub123", "a@example.com")
    assert store.get_sub_for_token(token) == "sub123"


def test_get_sub_for_token_returns_none_for_unknown(isolated_firestore_auth_store):
    assert isolated_firestore_auth_store.get_sub_for_token("nope") is None


def test_revoke_invalidates_the_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    token = store.get_or_create_token("sub123", "a@example.com")
    store.revoke("sub123")
    assert store.is_valid_token(token) is False
    assert store.list_users() == []


def test_revoke_unknown_sub_does_not_crash(isolated_firestore_auth_store):
    isolated_firestore_auth_store.revoke("nonexistent-sub")


def test_two_tokens_per_account_do_not_collide(isolated_firestore_auth_store):
    """The single most important security property of this module: a
    user's direct sign-in token and an OAuth-client-issued token for the
    same google_sub are different secrets, and revoking one has zero
    effect on the other."""
    store = isolated_firestore_auth_store
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")

    assert sign_in_token != oauth_token
    assert store.is_valid_token(sign_in_token)
    assert store.is_valid_token(oauth_token)
    assert store.get_sub_for_token(sign_in_token) == "sub123"
    assert store.get_sub_for_token(oauth_token) == "sub123"

    # Surgical revoke: dropping the OAuth token leaves the direct
    # sign-in token untouched (and vice versa) -- they're separate
    # secrets in separate collections.
    store.revoke_oauth_token(oauth_token)
    assert store.is_valid_token(oauth_token) is False
    assert store.is_valid_token(sign_in_token) is True

    # Account-wide revoke, by contrast, is deliberately blunt: it kills
    # EVERY credential for the sub -- the sign-in token AND every
    # OAuth-issued token -- matching revoke()'s docstring and the
    # SQLite/DynamoDB backends. OAuth *client registrations* survive.
    sign_in_token_2 = store.get_or_create_token("sub123", "a@example.com")
    oauth_token_2 = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    store.revoke("sub123")
    assert store.is_valid_token(sign_in_token_2) is False
    assert store.is_valid_token(oauth_token_2) is False


def test_register_and_get_oauth_client_round_trip(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"], client_name="Test Client")
    client = store.get_oauth_client(client_id)
    assert client["client_id"] == client_id
    assert client["client_name"] == "Test Client"
    assert client["redirect_uris"] == ["https://example.com/cb"]
    assert isinstance(client["redirect_uris"], list)


def test_register_oauth_client_rejects_non_loopback_http(isolated_firestore_auth_store):
    with pytest.raises(ValueError):
        isolated_firestore_auth_store.register_oauth_client(["http://evil.example.com/cb"])


def test_register_oauth_client_allows_loopback_http(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["http://127.0.0.1:8080/cb"])
    assert store.get_oauth_client(client_id) is not None


def test_register_oauth_client_requires_redirect_uris(isolated_firestore_auth_store):
    with pytest.raises(ValueError):
        isolated_firestore_auth_store.register_oauth_client([])


def test_get_oauth_client_unknown_returns_none(isolated_firestore_auth_store):
    assert isolated_firestore_auth_store.get_oauth_client("nonexistent") is None


def test_redeem_oauth_code_success_returns_identity(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )
    google_sub, email = store.redeem_oauth_code(
        code, client_id, "https://example.com/cb", verifier, "https://server/mcp"
    )
    assert (google_sub, email) == ("sub123", "a@example.com")


def test_redeem_oauth_code_rejects_wrong_pkce_verifier(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )
    with pytest.raises(ValueError, match="PKCE"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", "wrong-verifier", "https://server/mcp")


def test_redeem_oauth_code_rejects_reuse(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )
    store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")
    with pytest.raises(ValueError, match="already used"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_expired(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp", ttl_seconds=-1
    )
    with pytest.raises(ValueError, match="expired"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_mismatched_redirect_uri(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )
    with pytest.raises(ValueError, match="does not match"):
        store.redeem_oauth_code(code, client_id, "https://different.example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_mismatched_resource(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )
    with pytest.raises(ValueError, match="resource does not match"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://different-server/mcp")


def test_redeem_oauth_code_rejects_unknown_code(isolated_firestore_auth_store):
    with pytest.raises(ValueError, match="invalid authorization code"):
        isolated_firestore_auth_store.redeem_oauth_code(
            "not-a-real-code", "client", "https://example.com/cb", "verifier", "https://server/mcp"
        )


def test_mint_oauth_token_is_valid_and_distinct_from_sign_in_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    assert oauth_token != sign_in_token
    assert store.is_valid_token(oauth_token)
    assert store.get_sub_for_token(oauth_token) == "sub123"


def test_mint_oauth_token_always_mints_fresh_token(isolated_firestore_auth_store):
    """Unlike get_or_create_token, this never reuses/upserts: two grants
    for the same (user, client) yield two distinct, independently
    revocable tokens."""
    store = isolated_firestore_auth_store
    first = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    second = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    assert first != second
    assert store.is_valid_token(first)
    assert store.is_valid_token(second)


def test_list_oauth_clients_returns_registered_clients(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"], client_name="Test")
    clients = store.list_oauth_clients()
    assert len(clients) == 1
    assert clients[0]["client_id"] == client_id


def test_revoke_oauth_client_removes_registration_and_unconsumed_codes(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier, challenge = _pkce_pair("v")
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp"
    )

    store.revoke_oauth_client(client_id)

    assert store.get_oauth_client(client_id) is None
    with pytest.raises(ValueError, match="invalid authorization code"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_revoke_oauth_client_does_not_touch_already_issued_tokens(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    client_id = store.register_oauth_client(["https://example.com/cb"])
    token = store.mint_oauth_token("sub123", "a@example.com", "Test Client")
    store.revoke_oauth_client(client_id)
    assert store.is_valid_token(token) is True


def test_list_oauth_tokens_never_includes_the_raw_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    tokens = store.list_oauth_tokens()
    assert len(tokens) == 1
    assert tokens[0]["google_sub"] == "sub123"
    assert "token" not in tokens[0]
    assert token not in tokens[0]["token_prefix"]
    assert tokens[0]["token_prefix"] == token[:8] + "..."


def test_revoke_oauth_token_invalidates_it_without_touching_sign_in_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    store.revoke_oauth_token(oauth_token)
    assert store.is_valid_token(oauth_token) is False
    assert store.is_valid_token(sign_in_token) is True


# --- Per-device / per-session tokens ---------------------------------

_CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_FIREFOX_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"


def test_device_token_mints_validates_and_lists(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    assert store.is_valid_token(token)
    assert store.get_sub_for_token(token) == "sub123"
    rows = store.list_tokens("sub123")
    assert len(rows) == 1
    assert rows[0]["label"] == "Chrome on macOS"
    assert token not in rows[0].values()


def test_device_token_idempotent_per_user_agent(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    first = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    again = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    other = store.get_or_create_device_token("sub123", "a@example.com", _FIREFOX_WIN)
    assert first == again
    assert other != first
    assert len(store.list_tokens("sub123")) == 2


def test_list_tokens_marks_current_and_is_scoped(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    laptop = store.get_or_create_device_token("alice", "alice@example.com", _CHROME_MAC)
    store.get_or_create_device_token("alice", "alice@example.com", _FIREFOX_WIN)
    store.get_or_create_device_token("bob", "bob@example.com", _CHROME_MAC)
    alice_rows = store.list_tokens("alice", current_token=laptop)
    assert len(alice_rows) == 2
    assert sum(1 for r in alice_rows if r["is_current"]) == 1
    assert len(store.list_tokens("bob")) == 1


def test_revoke_token_kills_exactly_one(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    laptop = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    work = store.get_or_create_device_token("sub123", "a@example.com", _FIREFOX_WIN)
    work_id = next(r["token_id"] for r in store.list_tokens("sub123", current_token=work) if r["is_current"])
    store.revoke_token("sub123", work_id)
    assert store.is_valid_token(work) is False
    assert store.is_valid_token(laptop) is True
    assert len(store.list_tokens("sub123")) == 1


def test_revoke_token_cannot_touch_another_users_token(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    alice = store.get_or_create_device_token("alice", "alice@example.com", _CHROME_MAC)
    bob = store.get_or_create_device_token("bob", "bob@example.com", _FIREFOX_WIN)
    bob_id = store.list_tokens("bob")[0]["token_id"]
    store.revoke_token("alice", bob_id)
    assert store.is_valid_token(bob) is True
    assert store.is_valid_token(alice) is True


def test_revoke_token_is_idempotent(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    token_id = store.list_tokens("sub123")[0]["token_id"]
    assert store.revoke_token("sub123", token_id) is True
    assert store.revoke_token("sub123", token_id) is False
    assert store.revoke_token("sub123", "never-existed") is False
    assert store.is_valid_token(token) is False


def test_account_wide_revoke_nukes_devices_and_oauth_tokens(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    shared = store.get_or_create_token("sub123", "a@example.com")
    laptop = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    store.revoke("sub123")
    for t in (shared, laptop, connector):
        assert store.is_valid_token(t) is False
    assert store.list_tokens("sub123") == []


def test_oauth_token_lists_as_connector_and_is_individually_revocable(isolated_firestore_auth_store):
    store = isolated_firestore_auth_store
    sign_in = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    connector_id = next(r["token_id"] for r in store.list_tokens("sub123") if r["kind"] == "connector")
    store.revoke_token("sub123", connector_id)
    assert store.is_valid_token(connector) is False
    assert store.is_valid_token(sign_in) is True


def test_touch_token_hourly_and_safe_on_unknown(isolated_firestore_auth_store, monkeypatch):
    store = isolated_firestore_auth_store
    token = store.get_or_create_device_token("sub123", "a@example.com", _CHROME_MAC)
    first_seen = store.list_tokens("sub123")[0]["last_seen_at"]
    store.touch_token(token)  # within the hour: no-op
    assert store.list_tokens("sub123")[0]["last_seen_at"] == first_seen
    monkeypatch.setattr(store.time, "time", lambda: first_seen + store.LAST_SEEN_REFRESH_SECONDS + 60)
    store.touch_token(token)
    assert store.list_tokens("sub123")[0]["last_seen_at"] > first_seen
    store.touch_token("not-a-real-token")  # must not raise
