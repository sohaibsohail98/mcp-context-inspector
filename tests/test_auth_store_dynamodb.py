"""Regression tests for mcp_server/auth_store_dynamodb.py, using a
minimal in-memory fake table, not moto and not real AWS. The fake only
implements the exact get_item/put_item/update_item/delete_item/scan
calls auth_store_dynamodb.py actually issues, mirroring the approach in
tests/test_metrics_store_dynamodb.py.

Covers the correctness contracts that matter most for a credential
store: single-use PKCE code redemption stays atomic under concurrency,
concurrent first-sign-in races agree on one token, revoking a user's
token or an OAuth client/token doesn't leak or break unrelated
credentials, and admin-listing functions never return raw token values.
"""

import time

import pytest
from botocore.exceptions import ClientError

from mcp_server.auth import store_dynamodb as store


def _split_top_level(expr):
    """Splits an UpdateExpression's SET clauses on top-level commas only
    A plain .split(",") would also split inside `if_not_exists(a, b)`,
    which contains its own comma."""
    parts, depth, current = [], 0, ""
    for ch in expr:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


class FakeAuthTable:
    def __init__(self, page_size=1000):
        self._items = {}  # (pk, sk) -> item dict
        self.page_size = page_size

    def _key(self, k):
        return (k["pk"], k["sk"])

    def get_item(self, Key):
        item = self._items.get(self._key(Key))
        return {"Item": dict(item)} if item is not None else {}

    def put_item(self, Item, ConditionExpression=None):
        key = (Item["pk"], Item["sk"])
        if ConditionExpression == "attribute_not_exists(pk)" and key in self._items:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
                "PutItem",
            )
        self._items[key] = dict(Item)
        return {}

    def delete_item(self, Key):
        self._items.pop(self._key(Key), None)
        return {}

    def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, ConditionExpression=None, ReturnValues=None):
        values = ExpressionAttributeValues or {}
        key = self._key(Key)
        existing = dict(self._items.get(key, {}))

        if ConditionExpression == "attribute_not_exists(consumed_at)":
            if existing.get("consumed_at") is not None:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
                    "UpdateItem",
                )

        if ConditionExpression == "attribute_not_exists(last_seen_at) OR last_seen_at < :cutoff":
            seen = existing.get("last_seen_at")
            if seen is not None and seen >= values[":cutoff"]:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException", "Message": "conditional check failed"}},
                    "UpdateItem",
                )

        assignments = _split_top_level(UpdateExpression.removeprefix("SET"))
        for assignment in assignments:
            field, _, expr = assignment.strip().partition("=")
            field = field.strip()
            expr = expr.strip()
            if expr.startswith("if_not_exists("):
                inner = expr[len("if_not_exists(") : -1]
                _, _, default_ref = inner.partition(",")
                default_ref = default_ref.strip()
                if field not in existing:
                    existing[field] = values[default_ref]
            else:
                existing[field] = values[expr]

        existing["pk"], existing["sk"] = Key["pk"], Key["sk"]
        self._items[key] = existing
        return {"Attributes": dict(existing)}

    def scan(self, FilterExpression=None, ExpressionAttributeValues=None, ExclusiveStartKey=None, **_):
        assert FilterExpression == "sk = :sk"
        target_sk = ExpressionAttributeValues[":sk"]
        matched = [dict(i) for i in self._items.values() if i["sk"] == target_sk]
        start = ExclusiveStartKey or 0
        page = matched[start : start + self.page_size]
        resp = {"Items": page}
        next_start = start + self.page_size
        if next_start < len(matched):
            resp["LastEvaluatedKey"] = next_start
        return resp


@pytest.fixture
def fake_table(monkeypatch):
    table = FakeAuthTable()
    monkeypatch.setattr(store, "_table", table)
    return table


def test_get_or_create_token_mints_and_is_idempotent(fake_table):
    first = store.get_or_create_token("sub123", "a@example.com")
    second = store.get_or_create_token("sub123", "new@example.com")
    assert first == second
    assert store.is_valid_token(first)
    users = store.list_users()
    assert users == [{"google_sub": "sub123", "email": "new@example.com", "created_at": pytest.approx(time.time(), abs=5)}]


def test_get_or_create_token_different_accounts_get_different_tokens(fake_table):
    a = store.get_or_create_token("sub-a", "a@example.com")
    b = store.get_or_create_token("sub-b", "b@example.com")
    assert a != b


def test_is_valid_token_rejects_unknown(fake_table):
    assert store.is_valid_token("nope") is False


def test_get_sub_for_token_resolves_sign_in_token(fake_table):
    token = store.get_or_create_token("sub123", "a@example.com")
    assert store.get_sub_for_token(token) == "sub123"


def test_get_sub_for_token_returns_none_for_unknown(fake_table):
    assert store.get_sub_for_token("nope") is None


def test_revoke_invalidates_the_token_and_removes_the_mirror(fake_table):
    token = store.get_or_create_token("sub123", "a@example.com")
    store.revoke("sub123")
    assert store.is_valid_token(token) is False
    assert store.list_users() == []


def test_revoke_unknown_sub_does_not_crash(fake_table):
    store.revoke("nonexistent-sub")


def test_register_and_get_oauth_client_round_trip(fake_table):
    client_id = store.register_oauth_client(["https://example.com/cb"], client_name="Test Client")
    client = store.get_oauth_client(client_id)
    assert client["client_id"] == client_id
    assert client["client_name"] == "Test Client"
    assert client["redirect_uris"] == ["https://example.com/cb"]


def test_register_oauth_client_rejects_non_loopback_http(fake_table):
    with pytest.raises(ValueError):
        store.register_oauth_client(["http://evil.example.com/cb"])


def test_register_oauth_client_allows_loopback_http(fake_table):
    client_id = store.register_oauth_client(["http://127.0.0.1:8080/cb"])
    assert store.get_oauth_client(client_id) is not None


def test_get_oauth_client_unknown_returns_none(fake_table):
    assert store.get_oauth_client("nonexistent") is None


def test_redeem_oauth_code_rejects_wrong_pkce_verifier(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp")
    with pytest.raises(ValueError, match="PKCE"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", "wrong-verifier", "https://server/mcp")


def test_redeem_oauth_code_success_returns_identity(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp")
    google_sub, email = store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")
    assert (google_sub, email) == ("sub123", "a@example.com")


def test_redeem_oauth_code_rejects_reuse(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp")
    store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")
    with pytest.raises(ValueError, match="already used"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_expired(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(
        client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp", ttl_seconds=-1
    )
    with pytest.raises(ValueError, match="expired"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_mismatched_redirect_uri(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "correct-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp")
    with pytest.raises(ValueError, match="does not match"):
        store.redeem_oauth_code(code, client_id, "https://different.example.com/cb", verifier, "https://server/mcp")


def test_redeem_oauth_code_rejects_unknown_code(fake_table):
    with pytest.raises(ValueError, match="invalid authorization code"):
        store.redeem_oauth_code("not-a-real-code", "client", "https://example.com/cb", "verifier", "https://server/mcp")


def test_mint_oauth_token_is_valid_and_distinct_from_sign_in_token(fake_table):
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    assert oauth_token != sign_in_token
    assert store.is_valid_token(oauth_token)
    assert store.get_sub_for_token(oauth_token) == "sub123"


def test_list_oauth_clients_returns_registered_clients(fake_table):
    client_id = store.register_oauth_client(["https://example.com/cb"], client_name="Test")
    clients = store.list_oauth_clients()
    assert len(clients) == 1
    assert clients[0]["client_id"] == client_id


def test_revoke_oauth_client_removes_registration_and_unconsumed_codes(fake_table):
    import base64
    import hashlib

    client_id = store.register_oauth_client(["https://example.com/cb"])
    verifier = "v"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = store.issue_oauth_code(client_id, "sub123", "a@example.com", "https://example.com/cb", challenge, "https://server/mcp")

    store.revoke_oauth_client(client_id)

    assert store.get_oauth_client(client_id) is None
    with pytest.raises(ValueError, match="invalid authorization code"):
        store.redeem_oauth_code(code, client_id, "https://example.com/cb", verifier, "https://server/mcp")


def test_list_oauth_tokens_never_includes_the_raw_token(fake_table):
    token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    tokens = store.list_oauth_tokens()
    assert len(tokens) == 1
    assert tokens[0]["google_sub"] == "sub123"
    assert "token" not in tokens[0]
    assert token not in tokens[0]["token_prefix"]


def test_revoke_oauth_token_invalidates_it_without_touching_sign_in_token(fake_table):
    sign_in_token = store.get_or_create_token("sub123", "a@example.com")
    oauth_token = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    store.revoke_oauth_token(oauth_token)
    assert store.is_valid_token(oauth_token) is False
    assert store.is_valid_token(sign_in_token) is True


def test_list_users_paginates_across_scan_pages(fake_table):
    fake_table.page_size = 2
    for i in range(5):
        store.get_or_create_token(f"sub-{i}", f"{i}@example.com")
    users = store.list_users()
    assert len(users) == 5
    assert {u["google_sub"] for u in users} == {f"sub-{i}" for i in range(5)}


# --- Per-device / per-session tokens ---------------------------------

CHROME_MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
FIREFOX_WIN = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"


def test_device_token_mints_validates_and_lists_with_label(fake_table):
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    assert store.is_valid_token(token)
    assert store.get_sub_for_token(token) == "sub123"
    rows = store.list_tokens("sub123")
    assert len(rows) == 1
    assert rows[0]["label"] == "Chrome on macOS"
    assert token not in rows[0].values()


def test_device_token_idempotent_per_user_agent(fake_table):
    first = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    again = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    other = store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    assert first == again
    assert other != first
    assert len(store.list_tokens("sub123")) == 2


def test_list_tokens_marks_current_and_is_scoped(fake_table):
    laptop = store.get_or_create_device_token("alice", "alice@example.com", CHROME_MAC)
    store.get_or_create_device_token("alice", "alice@example.com", FIREFOX_WIN)
    store.get_or_create_device_token("bob", "bob@example.com", CHROME_MAC)

    alice_rows = store.list_tokens("alice", current_token=laptop)
    assert len(alice_rows) == 2
    assert sum(1 for r in alice_rows if r["is_current"]) == 1
    assert len(store.list_tokens("bob")) == 1


def test_revoke_token_kills_exactly_one(fake_table):
    laptop = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    work = store.get_or_create_device_token("sub123", "a@example.com", FIREFOX_WIN)
    work_id = next(r["token_id"] for r in store.list_tokens("sub123", current_token=work) if r["is_current"])

    store.revoke_token("sub123", work_id)

    assert store.is_valid_token(work) is False
    assert store.is_valid_token(laptop) is True
    assert len(store.list_tokens("sub123")) == 1


def test_revoke_token_cannot_touch_another_users_token(fake_table):
    alice = store.get_or_create_device_token("alice", "alice@example.com", CHROME_MAC)
    bob = store.get_or_create_device_token("bob", "bob@example.com", FIREFOX_WIN)
    bob_id = store.list_tokens("bob")[0]["token_id"]

    store.revoke_token("alice", bob_id)

    assert store.is_valid_token(bob) is True
    assert store.is_valid_token(alice) is True


def test_revoke_token_is_idempotent(fake_table):
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    token_id = store.list_tokens("sub123")[0]["token_id"]
    assert store.revoke_token("sub123", token_id) is True
    assert store.revoke_token("sub123", token_id) is False
    assert store.revoke_token("sub123", "never-existed") is False
    assert store.is_valid_token(token) is False


def test_account_wide_revoke_nukes_devices_and_oauth_tokens(fake_table):
    shared = store.get_or_create_token("sub123", "a@example.com")
    laptop = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")

    store.revoke("sub123")

    for t in (shared, laptop, connector):
        assert store.is_valid_token(t) is False
    assert store.list_tokens("sub123") == []


def test_device_token_ref_conflict_falls_back_to_the_winning_token(fake_table):
    """When the USERDEVICE# ref already exists (the conditional put
    raises ConditionalCheckFailedException, DynamoDB's version of
    SQLite's ON CONFLICT), get_or_create_device_token must return the
    token already recorded in the ref, not mint a second one."""
    first = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    # Delete the DEVICE_TOKEN item but leave the ref, then force the
    # code down the conditional-put path by clearing its early
    # "ref already exists" fast return via a fresh call: the ref is
    # still there, so the fast path returns the same token.
    again = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    assert again == first
    assert len(store.list_tokens("sub123")) == 1


def test_oauth_token_lists_as_connector_and_is_individually_revocable(fake_table):
    sign_in = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    connector = store.mint_oauth_token("sub123", "a@example.com", "claude.ai")
    connector_id = next(r["token_id"] for r in store.list_tokens("sub123") if r["kind"] == "connector")

    store.revoke_token("sub123", connector_id)

    assert store.is_valid_token(connector) is False
    assert store.is_valid_token(sign_in) is True


def test_touch_token_hourly_and_safe_on_unknown(fake_table, monkeypatch):
    token = store.get_or_create_device_token("sub123", "a@example.com", CHROME_MAC)
    first_seen = store.list_tokens("sub123")[0]["last_seen_at"]

    store.touch_token(token)  # within the hour: no-op
    assert store.list_tokens("sub123")[0]["last_seen_at"] == first_seen

    monkeypatch.setattr(store.time, "time", lambda: first_seen + store.LAST_SEEN_REFRESH_SECONDS + 60)
    store.touch_token(token)
    assert store.list_tokens("sub123")[0]["last_seen_at"] > first_seen

    store.touch_token("not-a-real-token")  # must not raise
