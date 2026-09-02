"""Firestore backend for the per-user MCP token store, for deployments
that already run on GCP/Firestore rather than DynamoDB. Same function
signatures and return shapes as auth/store_sqlite.py and
auth/store_dynamodb.py; callers go through auth/store.py's dispatcher.

Collections mirror the SQLite table names 1:1 (see store_sqlite.py's
module docstring for the full security reasoning behind this shape,
especially why oauth_tokens is a separate collection from mcp_users'
own `token` field):

- mcp_users/{google_sub}   : a signed-in user's own record (email,
                              token, created_at). One doc per Google
                              account; `google_sub` (the stable,
                              non-reassignable subject identifier from
                              the verified ID token) is the doc ID, not
                              email, which a user can change.
- oauth_clients/{client_id}: an OAuth Dynamic Client Registration
                              (RFC 7591). redirect_uris is a native
                              Firestore array field, not a JSON string
                              like the SQLite column, so no json.dumps/
                              loads needed on this backend.
- oauth_codes/{code_hash}  : a one-time authorization code. Keyed by
                              SHA-256 hash of the raw code, never the
                              raw code itself, same reasoning as
                              hashing a password: this collection being
                              readable shouldn't hand out usable codes.
- oauth_tokens/{token}     : an OAuth-issued access token, keyed by the
                              token value itself (already unique and
                              unguessable, so no extra ID needed).
                              Deliberately separate from mcp_users'
                              token field: disconnecting an OAuth client
                              (delete this doc) can't invalidate the
                              same user's direct Claude Code CLI token.

Uses google.cloud.firestore.Client() with Application Default
Credentials. On Cloud Run that's the attached service account; locally
(and in tests) set FIRESTORE_EMULATOR_HOST and the client library
transparently points at the emulator instead, no code-level
special-casing required.
"""

import base64
import hashlib
import secrets
import time
from urllib.parse import urlparse

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from mcp_server import local_setup
from mcp_server.auth.device_label import UNKNOWN_DEVICE, label_for_user_agent

# last_seen_at is only rewritten when it's older than this. On Firestore
# every touch is a document write, so refreshing on every authenticated
# request would multiply write cost by request rate for a field a human
# reads as an approximate "last seen"; an hour's granularity is plenty.
# Kept identical to the SQLite backend's constant so behaviour matches.
LAST_SEEN_REFRESH_SECONDS = 3600

_client = None


def _db():
    # Lazy singleton: constructing firestore.Client() talks to metadata
    # servers / reads ADC eagerly, which we don't want to pay for at
    # import time (e.g. for tests that only exercise other backends).
    # Same lazy-init reasoning as store_dynamodb.py's module-level
    # boto3 resource, just deferred one step further since Client() is
    # more expensive to construct than a boto3 Table handle.
    global _client
    if _client is None:
        _client = firestore.Client()
    return _client


def _users():
    return _db().collection("mcp_users")


def _clients():
    return _db().collection("oauth_clients")


def _codes():
    return _db().collection("oauth_codes")


def _tokens():
    return _db().collection("oauth_tokens")


def _device_tokens():
    return _db().collection("device_tokens")


def _install_codes():
    return _db().collection("install_codes")


def _sha256_hex(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _s256_challenge(code_verifier):
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def get_or_create_token(google_sub, email):
    """Returns this Google account's MCP token, minting one on first
    sign-in. Idempotent per google_sub: re-running the sign-in flow
    returns the same token, not a new one.

    Runs the read-then-create-or-update as a single Firestore
    transaction, the equivalent of SQLite's `INSERT ... ON CONFLICT DO
    UPDATE`: two concurrent first-time sign-ins for the same brand-new
    google_sub both start a transaction, read "no doc yet," and try to
    create one. Firestore serializes concurrent transactions that
    touch the same document, so one commits and the other automatically
    retries, sees the now-existing doc on its retry, and returns the
    token the winner created. Either way every caller agrees on one
    token, same as the SQLite path's final SELECT."""
    new_token = secrets.token_urlsafe(32)
    doc_ref = _users().document(google_sub)

    @firestore.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        if snapshot.exists:
            existing = snapshot.to_dict()
            if existing.get("email") != email:
                transaction.update(doc_ref, {"email": email})
            return existing["token"]
        transaction.set(doc_ref, {"email": email, "token": new_token, "created_at": time.time()})
        return new_token

    transaction = _db().transaction()
    return _txn(transaction)


def _device_token_doc(token):
    """The device_tokens doc for a raw token, or None.

    device_tokens docs are keyed by `{google_sub}:{sha256(user_agent)[:16]}`
    (so re-sign-in from the same device is a deterministic upsert; see
    get_or_create_device_token), NOT by the token value. So a bearer
    token has to be matched against the `token` FIELD via a query, the
    same way mcp_users is. (oauth_tokens, by contrast, IS keyed by the
    raw token, so those stay a direct .document(token).get().) An earlier
    version looked device tokens up with .document(token) here, which
    silently never matched -- device-token sign-in did not authenticate
    on the Firestore backend at all. Covered now by
    tests/test_auth_store_firestore.py running against the emulator."""
    matches = list(_device_tokens().where(filter=FieldFilter("token", "==", token)).limit(1).stream())
    return matches[0] if matches else None


def is_valid_token(token):
    """True for a direct Google-sign-in token (the shared mcp_users
    token, matched on its `token` field since the doc ID there is
    google_sub), a per-device sign-in token (device_tokens, also matched
    on its `token` field, see _device_token_doc), or an OAuth-flow token
    for a Connector-style client (oauth_tokens, keyed by the token
    itself). All are equally valid bearer credentials from the caller's
    perspective; only their provenance differs."""
    if list(_users().where(filter=FieldFilter("token", "==", token)).limit(1).stream()):
        return True
    if _device_token_doc(token) is not None:
        return True
    return _tokens().document(token).get().exists


def get_sub_for_token(token):
    """The google_sub that owns this token, used to attribute data
    (record/filter by owner) to whoever's actually connected, not just
    to check "is this token valid at all." Checks mcp_users,
    device_tokens and oauth_tokens (see is_valid_token). Returns None if
    the token doesn't belong to any signed-in user (e.g. it's invalid).

    MultiTokenAuthMiddleware calls this on every authenticated request
    (fronted by a short TTL cache, see mcp_server/auth/token_cache.py),
    and treats a None return as "invalid token" -- so this is both the
    ownership lookup and the validity check.
    """
    matches = list(_users().where(filter=FieldFilter("token", "==", token)).limit(1).stream())
    if matches:
        return matches[0].id  # doc ID IS google_sub in mcp_users
    device_doc = _device_token_doc(token)
    if device_doc is not None:
        return device_doc.to_dict()["google_sub"]
    snapshot = _tokens().document(token).get()
    return snapshot.to_dict()["google_sub"] if snapshot.exists else None


def list_users():
    """Admin visibility: who has ever signed in. Never returns tokens
    themselves, only enough to identify an account for revocation."""
    docs = _users().order_by("created_at").stream()
    return [
        {"google_sub": d.id, "email": d.to_dict().get("email"), "created_at": d.to_dict().get("created_at")}
        for d in docs
    ]


def revoke(google_sub):
    """Account-wide revoke: kills EVERY sign-in credential this user has
    minted, matching the SQLite backend. That's the shared mcp_users
    doc, every per-device doc in device_tokens, and every OAuth-issued
    access token for the sub. revoke_token() is the single-device
    version. OAuth client registrations are left alone."""
    _users().document(google_sub).delete()
    for d in _device_tokens().where(filter=FieldFilter("google_sub", "==", google_sub)).stream():
        d.reference.delete()
    for d in _tokens().where(filter=FieldFilter("google_sub", "==", google_sub)).stream():
        d.reference.delete()


# --- Per-device / per-session tokens ----------------------------------


def _token_id(token):
    """Stable public handle for a token: a SHA-256 hex prefix, never the
    raw token (it appears in URLs / logs / the dashboard DOM)."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def get_or_create_device_token(google_sub, email, user_agent):
    """Per-device sign-in token for (google_sub, this device), minting
    one on first sign-in from that device and returning the existing one
    on repeat sign-ins from the same User-Agent. Same contract as the
    SQLite backend.

    "Device" is a deterministic doc ID: <google_sub>:<sha256(UA)[:16]>,
    so the get-or-create is a single transactional read+write on one
    document (Firestore serialises concurrent transactions on the same
    doc, so a first-sign-in race resolves to one winning token, same as
    SQLite's ON CONFLICT)."""
    ua = user_agent or ""
    device_hash = hashlib.sha256(ua.encode()).hexdigest()[:16]
    doc_id = f"{google_sub}:{device_hash}"
    doc_ref = _device_tokens().document(doc_id)
    label = label_for_user_agent(user_agent)
    new_token = secrets.token_urlsafe(32)
    now = time.time()

    @firestore.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        if snapshot.exists:
            existing = snapshot.to_dict()
            updates = {}
            if existing.get("email") != email:
                updates["email"] = email
            if updates:
                transaction.update(doc_ref, updates)
            return existing["token"]
        transaction.set(
            doc_ref,
            {
                "token": new_token,
                "token_id": _token_id(new_token),
                "google_sub": google_sub,
                "email": email,
                "device_key": device_hash,
                "label": label,
                "created_at": now,
                "last_seen_at": now,
            },
        )
        return new_token

    transaction = _db().transaction()
    return _txn(transaction)


def touch_token(token):
    """Best-effort hourly refresh of last_seen_at for a device/OAuth
    token. Skips the write when the stored value is newer than
    LAST_SEEN_REFRESH_SECONDS. Never raises.

    Called at most once per token per token_cache.TTL_SECONDS from the
    auth middleware's cache-miss hook, not once per request."""
    now = time.time()
    cutoff = now - LAST_SEEN_REFRESH_SECONDS
    try:
        # device_tokens is keyed by {sub}:{ua_hash}, not the token, so it
        # must be matched on the `token` field (see _device_token_doc).
        device_doc = _device_token_doc(token)
        if device_doc is not None:
            if (device_doc.to_dict().get("last_seen_at") or 0) < cutoff:
                device_doc.reference.update({"last_seen_at": now})
            return
        # oauth_tokens IS keyed by the raw token.
        snap = _tokens().document(token).get()
        if snap.exists and (snap.to_dict().get("last_seen_at") or 0) < cutoff:
            snap.reference.update({"last_seen_at": now})
    except Exception:  # noqa: BLE001, S110 - a failed "last seen" refresh must not fail the request it decorates
        pass


def list_tokens(google_sub, current_token=None):
    """Every active per-device sign-in token and connector session for
    this account, newest first, as one unified list. Entry shape:
    {token_id, label, created_at, last_seen_at, is_current, kind}. Never
    returns a raw token. The shared mcp_users token is not listed (no
    per-device identity; account-wide revoke owns it)."""
    current_id = _token_id(current_token) if current_token else None
    out = []
    device_docs = _device_tokens().where(filter=FieldFilter("google_sub", "==", google_sub)).stream()
    for d in device_docs:
        t = d.to_dict()
        out.append(
            {
                "token_id": t.get("token_id"),
                "label": t.get("label") or UNKNOWN_DEVICE,
                "created_at": t.get("created_at"),
                "last_seen_at": t.get("last_seen_at"),
                "is_current": current_id is not None and t.get("token_id") == current_id,
                "kind": "device",
            }
        )
    oauth_docs = _tokens().where(filter=FieldFilter("google_sub", "==", google_sub)).stream()
    for d in oauth_docs:
        t = d.to_dict()
        label = t.get("label") or (f"{t.get('client_name')} (connector)" if t.get("client_name") else UNKNOWN_DEVICE)
        out.append(
            {
                "token_id": t.get("token_id"),
                "label": label,
                "created_at": t.get("created_at"),
                "last_seen_at": t.get("last_seen_at"),
                "is_current": current_id is not None
                and t.get("token_id") is not None
                and t.get("token_id") == current_id,
                "kind": "connector",
            }
        )
    out.sort(key=lambda r: r.get("created_at") or 0, reverse=True)
    return out


def revoke_token(google_sub, token_id):
    """Revoke exactly one device/session token by its public token_id,
    scoped to google_sub so a user can never revoke another user's.
    Idempotent (no-op if already gone). Returns True if a doc was
    deleted. Firestore has no unique secondary index here, so this is a
    filtered query + delete, fine at personal-project scale like every
    other query in this module."""
    if not token_id:
        return False
    deleted = False
    for coll in (_device_tokens(), _tokens()):
        docs = (
            coll.where(filter=FieldFilter("google_sub", "==", google_sub))
            .where(filter=FieldFilter("token_id", "==", token_id))
            .stream()
        )
        for d in docs:
            d.reference.delete()
            deleted = True
    return deleted


# --- OAuth 2.1 + PKCE authorization server ------------------------------
# Backs the /oauth/* routes in server.py; see that module's docstring for
# the flow. A generic, spec-compliant implementation: any MCP client that
# does OAuth discovery (Dynamic Client Registration + authorization code +
# PKCE) can use this, not just one specific product.


def register_oauth_client(redirect_uris, client_name=None, token_endpoint_auth_method="none"):
    """Dynamic Client Registration (RFC 7591). Mints a fresh client_id for
    a caller (an MCP client doing OAuth discovery) that presents at least
    one redirect URI. Raises ValueError on a malformed request; the route
    handler turns that into a 400."""
    if not redirect_uris:
        raise ValueError("redirect_uris is required")
    for uri in redirect_uris:
        parsed = urlparse(uri)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("redirect_uris must be absolute URLs")
        # A plain-http redirect_uri would let the one-time authorization
        # code (and transitively, via /oauth/token, the resulting access
        # token) travel in cleartext to anyone on-path between the
        # browser and that redirect target. Only allow it for a client
        # redirecting back to itself on the same machine (a CLI tool
        # completing its own loopback flow), the standard OAuth carve-out
        # for public clients that can't get a real TLS cert for localhost.
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("http redirect_uris are only allowed for localhost/127.0.0.1")

    client_id = secrets.token_urlsafe(24)
    _clients().document(client_id).set(
        {
            "client_name": client_name,
            "redirect_uris": list(redirect_uris),
            "token_endpoint_auth_method": token_endpoint_auth_method or "none",
            "created_at": time.time(),
        }
    )
    return client_id


def get_oauth_client(client_id):
    snapshot = _clients().document(client_id).get()
    if not snapshot.exists:
        return None
    client = snapshot.to_dict()
    client["client_id"] = client_id
    client["redirect_uris"] = list(client.get("redirect_uris") or [])
    return client


def issue_oauth_code(client_id, google_sub, email, redirect_uri, code_challenge, resource, ttl_seconds=600):
    """Mints a one-time authorization code for a client whose redirect_uri
    has already been validated against its registration by the /oauth/authorize
    route. Returns the raw code (shown to the caller exactly once); only
    its hash is stored."""
    raw = secrets.token_urlsafe(32)
    now = time.time()
    _codes().document(_sha256_hex(raw)).set(
        {
            "client_id": client_id,
            "google_sub": google_sub,
            "email": email,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "resource": resource,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "consumed_at": None,
        }
    )
    return raw


def redeem_oauth_code(code, client_id, redirect_uri, code_verifier, resource):
    """Validates and single-use-consumes an authorization code: right
    client, right redirect_uri, unexpired, unused, and the PKCE verifier
    actually hashes to the challenge that was presented at /oauth/authorize
    (this is what stops an attacker who intercepts the code, but not the
    verifier, which never left the original requester, from redeeming
    it). Also checks the resource (audience) matches, so a token minted
    here can't be replayed against a different MCP server. Raises
    ValueError with a caller-safe message on any failure; returns
    (google_sub, email) on success.

    All validation plus the final consume-write happens inside one
    Firestore transaction. This is what makes single-use enforcement
    race-safe: Firestore serializes concurrent transactions touching the
    same document, so of two concurrent redemptions of the same code,
    one commits (marking consumed_at) and the other is automatically
    retried by the client library. On retry it re-reads the doc, now
    sees consumed_at already set, and raises "authorization code already
    used" itself. This mirrors SQLite's `UPDATE ... WHERE consumed_at IS
    NULL` + rowcount check, and DynamoDB's conditional update, just
    achieved via transaction retry instead of an explicit conditional
    write."""
    code_hash = _sha256_hex(code)
    doc_ref = _codes().document(code_hash)

    @firestore.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise ValueError("invalid authorization code")
        row = snapshot.to_dict()
        now = time.time()
        if row.get("consumed_at") is not None:
            raise ValueError("authorization code already used")
        if row["expires_at"] < now:
            raise ValueError("authorization code expired")
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            raise ValueError("client_id or redirect_uri does not match")
        if row["resource"].rstrip("/") != resource.rstrip("/"):
            raise ValueError("resource does not match")
        if _s256_challenge(code_verifier) != row["code_challenge"]:
            raise ValueError("PKCE verification failed")

        transaction.update(doc_ref, {"consumed_at": now})
        return row["google_sub"], row["email"]

    transaction = _db().transaction()
    return _txn(transaction)


def issue_install_code(bearer_token, ttl_seconds=local_setup.INSTALL_CODE_TTL_SECONDS):
    """Mints a one-time code for the /setup/install curl-able one-liner.
    The page hands this to a signed-in caller instead of embedding their
    real bearer token in plaintext (which would otherwise end up in shell
    history forever). Stores bearer_token itself (not an identity to
    re-derive it from later), so it works identically for a per-user token or
    the shared owner token. Returns the raw code (shown exactly once);
    only its hash is stored, same reasoning as issue_oauth_code.
    Deliberately a separate collection from oauth_codes, with no
    client_id/redirect_uri/PKCE, since there's no OAuth client or browser
    redirect involved here."""
    raw = secrets.token_urlsafe(32)
    now = time.time()
    _install_codes().document(_sha256_hex(raw)).set(
        {
            "bearer_token": bearer_token,
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "consumed_at": None,
        }
    )
    return raw


def redeem_install_code(code):
    """Validates and single-use-consumes an install code, same
    transactional single-use guarantee as redeem_oauth_code (see that
    function's docstring for why the consume happens inside the same
    transaction as the checks). Raises ValueError with a caller-safe
    message; returns the bearer token on success."""
    code_hash = _sha256_hex(code)
    doc_ref = _install_codes().document(code_hash)

    @firestore.transactional
    def _txn(transaction):
        snapshot = doc_ref.get(transaction=transaction)
        if not snapshot.exists:
            raise ValueError("invalid install code")
        row = snapshot.to_dict()
        now = time.time()
        if row.get("consumed_at") is not None:
            raise ValueError("install code already used")
        if row["expires_at"] < now:
            raise ValueError("install code expired")

        transaction.update(doc_ref, {"consumed_at": now})
        return row["bearer_token"]

    transaction = _db().transaction()
    return _txn(transaction)


def list_oauth_clients():
    """Admin visibility: every OAuth client that's ever completed Dynamic
    Client Registration. No client secret to redact (token_endpoint_auth_method
    is always "none": these are public clients, and PKCE is the actual
    protection), so this is safe to return in full."""
    docs = _clients().order_by("created_at").stream()
    clients = []
    for d in docs:
        c = d.to_dict()
        c["client_id"] = d.id
        c["redirect_uris"] = list(c.get("redirect_uris") or [])
        clients.append(c)
    return clients


def revoke_oauth_client(client_id):
    """Deletes a client's registration and any of its outstanding
    (unconsumed) authorization codes, so it can no longer complete a new
    /oauth/authorize -> /oauth/token exchange. Does NOT retroactively
    invalidate access tokens this client already obtained. oauth_tokens
    only records client_name (a caller-supplied display string, not
    guaranteed unique per client_id), so there's no safe way to map a
    token back to the exact client that requested it. Revoke those
    individually with revoke_oauth_token() instead; see list_oauth_tokens()
    to find them."""
    _clients().document(client_id).delete()
    unconsumed = (
        _codes()
        .where(filter=FieldFilter("client_id", "==", client_id))
        .where(filter=FieldFilter("consumed_at", "==", None))
        .stream()
    )
    for d in unconsumed:
        d.reference.delete()


def list_oauth_tokens():
    """Admin visibility: every access token issued via the OAuth flow.
    Never returns the token value itself, only enough to identify and
    revoke it (see revoke_oauth_token)."""
    docs = _tokens().order_by("created_at").stream()
    tokens = []
    for d in docs:
        t = d.to_dict()
        tokens.append(
            {
                "google_sub": t.get("google_sub"),
                "email": t.get("email"),
                "client_name": t.get("client_name"),
                "created_at": t.get("created_at"),
                "token_prefix": d.id[:8] + "...",
            }
        )
    return tokens


def revoke_oauth_token(token):
    """Deletes one OAuth-issued access token. The client that held it
    needs to redo the full authorization flow to get a new one. Doesn't
    touch mcp_users' own token (see revoke()), so this can't accidentally
    break a user's direct Claude Code config while disconnecting one
    Connector-style client."""
    _tokens().document(token).delete()


def mint_oauth_token(google_sub, email, client_name, user_agent=None):
    """A fresh access token for one (user, OAuth client) pair. Deliberately
    not the same token get_or_create_token returns, so disconnecting this
    client later doesn't also invalidate the user's direct MCP client
    config. Always mints a new token, unlike get_or_create_token: a second
    OAuth authorization for the same client is a re-consent, not a replay,
    and each grant gets its own revocable credential.

    Records per-session metadata (token_id, label, last_seen_at) so this
    grant appears in list_tokens() alongside direct-sign-in devices; see
    the SQLite backend's docstring for why user_agent is best-effort."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    ua_label = label_for_user_agent(user_agent)
    label = (
        ua_label if ua_label != UNKNOWN_DEVICE else (f"{client_name} (connector)" if client_name else UNKNOWN_DEVICE)
    )
    _tokens().document(token).set(
        {
            "token_id": _token_id(token),
            "google_sub": google_sub,
            "email": email,
            "client_name": client_name,
            "label": label,
            "created_at": now,
            "last_seen_at": now,
        }
    )
    return token
