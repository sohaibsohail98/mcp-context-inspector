"""DynamoDB backend for the per-user MCP token store — the auth
equivalent of metrics/store_dynamodb.py, same reasoning: a Cloud Run
container's local filesystem doesn't persist across cold starts or
survive multiple concurrent instances each with their own SQLite file.
Same function signatures as auth/store_sqlite.py; callers go through
auth/store.py's dispatcher.

Single-table design, mirroring the metrics table's approach:
partition key `pk`, sort key `sk`.

- USER#<google_sub>  / PROFILE     — a signed-in user's own record
- USERTOKEN#<token>  / USER_REF    — reverse-lookup mirror of a user's
                                      token -> google_sub, so
                                      is_valid_token/get_sub_for_token
                                      don't need a GSI. Kept in sync
                                      with PROFILE by get_or_create_token
                                      and revoke(); see those functions'
                                      docstrings for the consistency
                                      tradeoffs this implies.
- CLIENT#<client_id> / REGISTRATION — an OAuth Dynamic Client Registration
- CODE#<code_hash>   / AUTH_CODE    — a one-time authorization code
                                      (also carries a `ttl` attribute for
                                      DynamoDB's native expiry as
                                      best-effort cleanup; redeem_oauth_code
                                      still checks expires_at explicitly,
                                      same as the SQLite backend, since
                                      TTL deletion isn't instantaneous)
- TOKEN#<token>      / ACCESS_TOKEN — an OAuth-issued access token; its
                                      own key already IS the token, so no
                                      reverse index is needed for this one

Aggregate reads (list_users, list_oauth_clients, list_oauth_tokens) use
Scan with an sk filter — fine at personal-project scale, same tradeoff
already accepted in metrics/store_dynamodb.py.
"""

import os
import time
import uuid

import boto3

from mci_common.config import DEFAULT_REGION
from mcp_server import local_setup

TABLE_NAME = os.environ.get("AUTH_TABLE", "mcp-context-auth")
REGION = os.environ.get("AWS_REGION", DEFAULT_REGION)

_table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)


def _user_key(google_sub):
    return {"pk": f"USER#{google_sub}", "sk": "PROFILE"}


def _user_token_key(token):
    return {"pk": f"USERTOKEN#{token}", "sk": "USER_REF"}


def _client_key(client_id):
    return {"pk": f"CLIENT#{client_id}", "sk": "REGISTRATION"}


def _code_key(code_hash):
    return {"pk": f"CODE#{code_hash}", "sk": "AUTH_CODE"}


def _oauth_token_key(token):
    return {"pk": f"TOKEN#{token}", "sk": "ACCESS_TOKEN"}


def _install_code_key(code_hash):
    return {"pk": f"INSTALLCODE#{code_hash}", "sk": "INSTALL_CODE"}


def get_or_create_token(google_sub, email):
    """Returns this Google account's MCP token, minting one on first
    sign-in — same contract as the SQLite version, including
    idempotency under concurrent first-time sign-ins.

    `if_not_exists(token, :new_token)` in a single update_item call is
    DynamoDB's equivalent of SQLite's `ON CONFLICT DO UPDATE`: two
    concurrent calls for a brand-new google_sub both race to set the
    attribute, but only the first write actually "wins" the token value
    (DynamoDB serializes concurrent updates to the same item), and both
    calls' ReturnValues=ALL_NEW response reflects whichever value
    actually won — so both callers agree on one token, same as the
    SQLite path's final SELECT.

    The USERTOKEN# mirror write happens second, using the token that
    actually won the race (not necessarily the one this call generated)
    — there's a small window where PROFILE exists but USERTOKEN# doesn't
    yet if the process crashes between the two writes; is_valid_token
    would then reject a real token until this function runs again for
    the same account (self-healing, not a silent security hole)."""
    resolved_token = str(uuid.uuid4()).replace("-", "") + str(uuid.uuid4()).replace("-", "")
    now = time.time()
    resp = _table.update_item(
        Key=_user_key(google_sub),
        UpdateExpression="SET email = :email, token = if_not_exists(token, :new_token), created_at = if_not_exists(created_at, :now)",
        ExpressionAttributeValues={":email": email, ":new_token": resolved_token, ":now": now},
        ReturnValues="ALL_NEW",
    )
    winning_token = resp["Attributes"]["token"]
    _table.put_item(
        Item={**_user_token_key(winning_token), "google_sub": google_sub, "email": email},
    )
    return winning_token


def is_valid_token(token):
    """True for either a direct Google-sign-in token (USERTOKEN# mirror)
    or a token minted through the OAuth flow (its own TOKEN# item) —
    same either-source contract as the SQLite version."""
    if _table.get_item(Key=_user_token_key(token)).get("Item"):
        return True
    return bool(_table.get_item(Key=_oauth_token_key(token)).get("Item"))


def get_sub_for_token(token):
    """The google_sub that owns this token, checking both sources — see
    is_valid_token. Returns None if the token doesn't belong to any
    signed-in user."""
    item = _table.get_item(Key=_user_token_key(token)).get("Item")
    if item:
        return item["google_sub"]
    item = _table.get_item(Key=_oauth_token_key(token)).get("Item")
    return item["google_sub"] if item else None


def list_users():
    """Admin visibility — who has ever signed in. Never returns tokens
    themselves, only enough to identify an account for revocation."""
    items = _scan_by_sk("PROFILE")
    return [
        {"google_sub": i["pk"].removeprefix("USER#"), "email": i.get("email"), "created_at": i.get("created_at")}
        for i in items
    ]


def revoke(google_sub):
    """Deletes a user's token — they'd need to sign in again to get a
    new one. Reads the PROFILE item first to find the matching
    USERTOKEN# mirror so both are removed; a crash between the two
    deletes could leave an orphaned, still-valid USERTOKEN# item behind
    (rare, and the next revoke() call for the same account cleans it up
    too, since the read-then-delete is idempotent on a missing item)."""
    profile = _table.get_item(Key=_user_key(google_sub)).get("Item")
    _table.delete_item(Key=_user_key(google_sub))
    if profile and profile.get("token"):
        _table.delete_item(Key=_user_token_key(profile["token"]))


# --- OAuth 2.1 + PKCE authorization server ------------------------------


def register_oauth_client(redirect_uris, client_name=None, token_endpoint_auth_method="none"):
    """Same validation as the SQLite version — see that module's
    docstring for why. Kept identical here rather than shared, since the
    validation is pure Python with no DB dependency and duplicating a
    handful of lines is simpler than a shared-helper module just for
    this."""
    from urllib.parse import urlparse

    if not redirect_uris:
        raise ValueError("redirect_uris is required")
    for uri in redirect_uris:
        parsed = urlparse(uri)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("redirect_uris must be absolute URLs")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("http redirect_uris are only allowed for localhost/127.0.0.1")

    client_id = str(uuid.uuid4()).replace("-", "")
    _table.put_item(
        Item={
            **_client_key(client_id),
            "client_id": client_id,
            "client_name": client_name,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "created_at": time.time(),
        }
    )
    return client_id


def get_oauth_client(client_id):
    item = _table.get_item(Key=_client_key(client_id)).get("Item")
    if item is None:
        return None
    return {
        "client_id": item["client_id"],
        "client_name": item.get("client_name"),
        "redirect_uris": list(item["redirect_uris"]),
        "token_endpoint_auth_method": item["token_endpoint_auth_method"],
        "created_at": item["created_at"],
    }


def issue_oauth_code(client_id, google_sub, email, redirect_uri, code_challenge, resource, ttl_seconds=600):
    """Mints a one-time authorization code — see the SQLite version's
    docstring for the full contract. code_hash (not the raw code) is the
    partition key here too, same reasoning: this table being readable
    shouldn't hand out usable codes."""
    import hashlib
    import secrets

    raw = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = time.time()
    expires_at = now + ttl_seconds
    _table.put_item(
        Item={
            **_code_key(code_hash),
            "client_id": client_id,
            "google_sub": google_sub,
            "email": email,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "resource": resource,
            "created_at": now,
            "expires_at": expires_at,
            "ttl": int(expires_at) + 3600,  # DynamoDB TTL cleanup, 1h grace past explicit expiry
        }
    )
    return raw


def redeem_oauth_code(code, client_id, redirect_uri, code_verifier, resource):
    """Validates and single-use-consumes an authorization code — same
    checks, same error messages, and the same atomicity guarantee as the
    SQLite version: the final consume step is a conditional update_item
    (`ConditionExpression="attribute_not_exists(consumed_at)"`), DynamoDB's
    equivalent of SQLite's `UPDATE ... WHERE consumed_at IS NULL`. Two
    concurrent redemptions of the same code both pass the read-side
    checks below, but only one conditional update can succeed — the
    loser gets a ConditionalCheckFailedException, caught and turned into
    the same ValueError the SQLite path raises for a reused code."""
    import base64
    import hashlib

    from botocore.exceptions import ClientError

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    row = _table.get_item(Key=_code_key(code_hash)).get("Item")
    if row is None:
        raise ValueError("invalid authorization code")
    now = time.time()
    if row.get("consumed_at") is not None:
        raise ValueError("authorization code already used")
    if row["expires_at"] < now:
        raise ValueError("authorization code expired")
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        raise ValueError("client_id or redirect_uri does not match")
    if row["resource"].rstrip("/") != resource.rstrip("/"):
        raise ValueError("resource does not match")
    digest = hashlib.sha256(code_verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    if challenge != row["code_challenge"]:
        raise ValueError("PKCE verification failed")

    try:
        _table.update_item(
            Key=_code_key(code_hash),
            UpdateExpression="SET consumed_at = :now",
            ConditionExpression="attribute_not_exists(consumed_at)",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError("authorization code already used") from e
        raise
    return row["google_sub"], row["email"]


def issue_install_code(bearer_token, ttl_seconds=local_setup.INSTALL_CODE_TTL_SECONDS):
    """Mints a one-time code for the /setup/install curl-able one-liner —
    see the SQLite version's docstring for the full contract. Stores
    bearer_token itself (not an identity to re-derive it from later).
    Deliberately a separate item shape from CODE#/AUTH_CODE — no
    client_id/redirect_uri/PKCE, since there's no OAuth client or
    browser redirect involved."""
    import hashlib
    import secrets

    raw = secrets.token_urlsafe(32)
    code_hash = hashlib.sha256(raw.encode()).hexdigest()
    now = time.time()
    expires_at = now + ttl_seconds
    _table.put_item(
        Item={
            **_install_code_key(code_hash),
            "bearer_token": bearer_token,
            "created_at": now,
            "expires_at": expires_at,
            "ttl": int(expires_at) + 3600,  # DynamoDB TTL cleanup, 1h grace past explicit expiry
        }
    )
    return raw


def redeem_install_code(code):
    """Validates and single-use-consumes an install code — same
    conditional-update atomicity as redeem_oauth_code (see that
    function's docstring). Returns the bearer token on success."""
    import hashlib

    from botocore.exceptions import ClientError

    code_hash = hashlib.sha256(code.encode()).hexdigest()
    row = _table.get_item(Key=_install_code_key(code_hash)).get("Item")
    if row is None:
        raise ValueError("invalid install code")
    now = time.time()
    if row.get("consumed_at") is not None:
        raise ValueError("install code already used")
    if row["expires_at"] < now:
        raise ValueError("install code expired")

    try:
        _table.update_item(
            Key=_install_code_key(code_hash),
            UpdateExpression="SET consumed_at = :now",
            ConditionExpression="attribute_not_exists(consumed_at)",
            ExpressionAttributeValues={":now": now},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ValueError("install code already used") from e
        raise
    return row["bearer_token"]


def mint_oauth_token(google_sub, email, client_name):
    """A fresh access token for one (user, OAuth client) pair — always a
    new token, same contract as the SQLite version."""
    import secrets

    token = secrets.token_urlsafe(32)
    _table.put_item(
        Item={
            **_oauth_token_key(token),
            "google_sub": google_sub,
            "email": email,
            "client_name": client_name,
            "created_at": time.time(),
        }
    )
    return token


def list_oauth_clients():
    """Admin visibility — every registered OAuth client."""
    items = _scan_by_sk("REGISTRATION")
    return [
        {
            "client_id": i["client_id"],
            "client_name": i.get("client_name"),
            "redirect_uris": list(i["redirect_uris"]),
            "token_endpoint_auth_method": i["token_endpoint_auth_method"],
            "created_at": i["created_at"],
        }
        for i in items
    ]


def revoke_oauth_client(client_id):
    """Deletes a client's registration and any of its outstanding
    (unconsumed) authorization codes — see the SQLite version's
    docstring for the same client_name-vs-client_id limitation on
    already-issued tokens. The unconsumed-codes cleanup is a Scan+filter
    (codes aren't keyed by client_id), fine at personal-project scale
    like every other Scan in this module."""
    _table.delete_item(Key=_client_key(client_id))
    for item in _scan_by_sk("AUTH_CODE"):
        if item.get("client_id") == client_id and item.get("consumed_at") is None:
            _table.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})


def list_oauth_tokens():
    """Admin visibility — every OAuth-issued access token. Never returns
    the token value itself, only enough to identify and revoke it."""
    items = _scan_by_sk("ACCESS_TOKEN")
    return [
        {
            "google_sub": i["google_sub"],
            "email": i.get("email"),
            "client_name": i.get("client_name"),
            "created_at": i["created_at"],
            "token_prefix": i["pk"].removeprefix("TOKEN#")[:8] + "...",
        }
        for i in items
    ]


def revoke_oauth_token(token):
    """Deletes one OAuth-issued access token — doesn't touch a user's
    own sign-in token (USER#/USERTOKEN# items), same isolation the
    SQLite version guarantees via its separate table."""
    _table.delete_item(Key=_oauth_token_key(token))


def _scan_by_sk(sk):
    """Paginated Scan filtered to one item type — see
    metrics/store_dynamodb.py's own pagination comment for why an
    unpaginated Scan silently undercounts as a table grows past one
    page; same fix applied here."""
    items = []
    kwargs = {"FilterExpression": "sk = :sk", "ExpressionAttributeValues": {":sk": sk}}
    while True:
        resp = _table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
