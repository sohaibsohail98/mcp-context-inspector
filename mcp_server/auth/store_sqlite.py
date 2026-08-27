"""SQLite backend for the per-user MCP token store, the local-dev /
default backend. Reached through auth/store.py's dispatcher
(STORAGE_BACKEND env var); import directly only for the DB_PATH test
hook (tests/conftest.py's isolated_auth_store fixture). This backend's
writes are lost on Cloud Run cold start; see auth/store_dynamodb.py
for the persistent one.

One row per Google account, keyed on `google_sub` (the stable,
non-reassignable subject identifier from the verified ID token, never
the email, which a user can change).

Also stores the OAuth 2.1 + PKCE authorization-server data (client
registrations, one-time authorization codes, per-client access tokens)
that backs the /oauth/* routes in mcp_server/routes/oauth.py.
"""

import base64
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

from mcp_server import local_setup
from mcp_server.auth.device_label import UNKNOWN_DEVICE, label_for_user_agent

DB_PATH = Path(__file__).parent.parent / "data" / "mcp_auth.db"

# last_seen_at is only rewritten when it's older than this, so a busy
# token doesn't cause a DB write on every single authenticated request.
# An hour is precise enough for a "last seen" column a human reads in a
# device list, and keeps write amplification identical across all three
# backends (Firestore/DynamoDB would otherwise pay a write per request).
# See touch_token().
LAST_SEEN_REFRESH_SECONDS = 3600

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS mcp_users (
        google_sub TEXT PRIMARY KEY,
        email TEXT,
        token TEXT UNIQUE,
        created_at REAL
    );

    -- Per-device / per-session sign-in tokens. One row per (google_sub,
    -- device): signing in from a new browser or a new machine mints a
    -- fresh row here, so a user can later revoke exactly one device
    -- ("revoke my work laptop") without touching the others. SEPARATE
    -- from mcp_users.token, which stays as the single shared account
    -- token for backwards compatibility (tokens pasted into an MCP
    -- client config before this table existed still validate via
    -- mcp_users; they just show as "Unknown device" in the UI since they
    -- have no row here).
    --
    -- token_id (a SHA-256 prefix of the token) is the stable public
    -- handle used by list_tokens/revoke_token; the raw token is never
    -- the id, same reasoning as hashing an OAuth code. device_key is
    -- SHA-256(google_sub | user_agent): re-signing-in from the same
    -- browser reuses that device's row and token rather than piling up a
    -- new token per page load.
    CREATE TABLE IF NOT EXISTS device_tokens (
        token TEXT PRIMARY KEY,
        token_id TEXT UNIQUE,
        google_sub TEXT,
        email TEXT,
        device_key TEXT,
        label TEXT,
        created_at REAL,
        last_seen_at REAL,
        UNIQUE (google_sub, device_key)
    );

    -- OAuth clients registered via POST /oauth/register (RFC 7591 Dynamic
    -- Client Registration). For example, claude.ai registers itself here once,
    -- the first time a user tries to add this server as a Connector.
    CREATE TABLE IF NOT EXISTS oauth_clients (
        client_id TEXT PRIMARY KEY,
        client_name TEXT,
        redirect_uris TEXT,  -- JSON list
        token_endpoint_auth_method TEXT,
        created_at REAL
    );

    -- One-time authorization codes (10-minute TTL, single-use) minted by
    -- GET/POST /oauth/authorize after the user signs in with Google, and
    -- redeemed by POST /oauth/token. code_hash, not the raw code, is
    -- stored, same reasoning as hashing a password: a code is a bearer
    -- secret, and this table being readable shouldn't hand out usable
    -- codes. SHA-256 (not a slow KDF like bcrypt) is enough here since
    -- the code is a 256-bit random secret, not a low-entropy password.
    -- This matches how this project already treats bearer tokens elsewhere.
    CREATE TABLE IF NOT EXISTS oauth_codes (
        code_hash TEXT PRIMARY KEY,
        client_id TEXT,
        google_sub TEXT,
        email TEXT,
        redirect_uri TEXT,
        code_challenge TEXT,
        resource TEXT,
        created_at REAL,
        expires_at REAL,
        consumed_at REAL
    );

    -- Access tokens minted at the end of the OAuth flow. Deliberately a
    -- SEPARATE table from mcp_users.token, one row per (google_sub,
    -- client) rather than reusing that single sign-in token: an OAuth
    -- client (claude.ai, say) gets its own named token, so disconnecting
    -- it later (DELETE FROM oauth_tokens) can't also break the same
    -- user's direct Claude Code config, which uses the mcp_users token.
    CREATE TABLE IF NOT EXISTS oauth_tokens (
        token TEXT PRIMARY KEY,
        google_sub TEXT,
        email TEXT,
        client_name TEXT,
        created_at REAL,
        -- Per-session metadata, mirrored from device_tokens' columns so
        -- list_tokens() can present connector sessions and direct-sign-in
        -- devices in one unified list. token_id is a SHA-256 prefix used
        -- as the public revoke handle; label is User-Agent-derived at
        -- mint time; last_seen_at is refreshed at most hourly (see
        -- touch_token). All three are nullable: a token minted before
        -- these columns existed reads back as "Unknown device".
        token_id TEXT,
        label TEXT,
        last_seen_at REAL
    );

    -- One-time install codes: the short-lived code /setup/install's
    -- curl-able one-liner exchanges for the caller's real bearer token,
    -- so the token itself never sits in shell history via a plaintext
    -- curl arg. Separate from oauth_codes, with no client_id/redirect_uri/
    -- PKCE, since there's no OAuth client or browser redirect involved.
    -- Same single-use + TTL + hash-keyed storage for the same reasons.
    CREATE TABLE IF NOT EXISTS install_codes (
        code_hash TEXT PRIMARY KEY,
        bearer_token TEXT,
        created_at REAL,
        expires_at REAL,
        consumed_at REAL
    );
"""


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    """Additive column backfill for a pre-existing data/mcp_auth.db.
    `CREATE TABLE IF NOT EXISTS` in _SCHEMA leaves an already-created
    oauth_tokens untouched, so the per-session metadata columns added
    for the device list are ADDed here if missing. Cheap (one PRAGMA per
    connect), a no-op on a fresh DB where _SCHEMA already made the
    columns. Old rows keep NULL in the new columns and read back as
    "Unknown device"."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(oauth_tokens)")}
    for col in ("token_id", "label", "last_seen_at"):
        if col not in have:
            conn.execute(f"ALTER TABLE oauth_tokens ADD COLUMN {col}")


def get_or_create_token(google_sub, email):
    """Returns this Google account's MCP token, minting one on first
    sign-in. Idempotent per google_sub: re-running the sign-in flow
    returns the same token, not a new one.

    A single atomic upsert, not a SELECT-then-INSERT: two concurrent
    first-time sign-ins for the same brand-new google_sub would both
    pass a "no existing row" check and the loser's INSERT would crash on
    the PRIMARY KEY constraint. ON CONFLICT DO UPDATE makes the loser a
    no-op, and the final SELECT returns whichever token won."""
    conn = _connect()
    conn.execute(
        "INSERT INTO mcp_users (google_sub, email, token, created_at) VALUES (?,?,?,?) "
        "ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email",
        (google_sub, email, secrets.token_urlsafe(32), time.time()),
    )
    conn.commit()
    row = conn.execute("SELECT token FROM mcp_users WHERE google_sub=?", (google_sub,)).fetchone()
    conn.close()
    return row["token"]


def is_valid_token(token):
    """True for a direct Google-sign-in token (the shared mcp_users
    token OR a per-device row in device_tokens) or a token minted
    through the OAuth flow for a Connector-style client (oauth_tokens).
    All are equally valid bearer credentials from the caller's
    perspective; only their provenance differs."""
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM mcp_users WHERE token=? "
        "UNION SELECT 1 FROM device_tokens WHERE token=? "
        "UNION SELECT 1 FROM oauth_tokens WHERE token=?",
        (token, token, token),
    ).fetchone()
    conn.close()
    return row is not None


def get_sub_for_token(token):
    """The google_sub that owns this token, used to attribute data
    (record/filter by owner) to whoever's actually connected, not just to
    check "is this token valid at all." Checks mcp_users, device_tokens
    and oauth_tokens (see is_valid_token). Returns None if the token doesn't
    belong to any signed-in user (e.g. it's the owner token, or invalid)."""
    conn = _connect()
    row = conn.execute("SELECT google_sub FROM mcp_users WHERE token=?", (token,)).fetchone()
    if row is None:
        row = conn.execute("SELECT google_sub FROM device_tokens WHERE token=?", (token,)).fetchone()
    if row is None:
        row = conn.execute("SELECT google_sub FROM oauth_tokens WHERE token=?", (token,)).fetchone()
    conn.close()
    return row["google_sub"] if row else None


def list_users():
    """Admin visibility: who has ever signed in. Never returns tokens
    themselves, only enough to identify an account for revocation."""
    conn = _connect()
    rows = conn.execute(
        "SELECT google_sub, email, created_at FROM mcp_users ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke(google_sub):
    """Account-wide revoke: kills EVERY sign-in credential this user has
    ever minted, so they'd need to sign in again on every device to get
    new ones. That's the shared mcp_users token, every per-device row in
    device_tokens, AND every OAuth-issued access token for the sub.
    Deliberately blunt; revoke_token() is the surgical single-device
    version. OAuth CLIENT registrations are left alone (they're not
    credentials, and are shared across users)."""
    conn = _connect()
    conn.execute("DELETE FROM mcp_users WHERE google_sub=?", (google_sub,))
    conn.execute("DELETE FROM device_tokens WHERE google_sub=?", (google_sub,))
    conn.execute("DELETE FROM oauth_tokens WHERE google_sub=?", (google_sub,))
    conn.commit()
    conn.close()


# --- Per-device / per-session tokens ----------------------------------
# device_tokens rows are minted at Google sign-in (routes/auth.py's
# auth_verify); OAuth-flow tokens land in oauth_tokens with the same
# metadata columns. list_tokens/revoke_token present and act on both as
# one list, always scoped by google_sub so a user can only ever see or
# revoke their own.


def _token_id(token):
    """Stable public handle for a token: a SHA-256 hex prefix. Never the
    raw token (this value appears in URLs, logs, and the dashboard DOM),
    long enough that a collision across one account's handful of devices
    is not a practical concern."""
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def get_or_create_device_token(google_sub, email, user_agent):
    """Returns a per-device sign-in token for (google_sub, this device),
    minting one on first sign-in from that device. "Device" is keyed on
    SHA-256(google_sub | user_agent): re-signing-in from the same
    browser/CLI returns the SAME token (so a config already pasted
    elsewhere isn't invalidated by a page reload), a new User-Agent gets
    its own token and its own row in the device list.

    Atomic upsert on the (google_sub, device_key) unique constraint,
    same race reasoning as get_or_create_token: two concurrent
    first-time sign-ins from one device both try to INSERT, the loser's
    ON CONFLICT DO UPDATE is a harmless no-op, and the final SELECT
    returns the winning token."""
    device_key = hashlib.sha256(f"{google_sub}|{user_agent or ''}".encode()).hexdigest()
    label = label_for_user_agent(user_agent)
    new_token = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    conn.execute(
        "INSERT INTO device_tokens "
        "(token, token_id, google_sub, email, device_key, label, created_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(google_sub, device_key) DO UPDATE SET email=excluded.email, last_seen_at=excluded.last_seen_at",
        (new_token, _token_id(new_token), google_sub, email, device_key, label, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT token FROM device_tokens WHERE google_sub=? AND device_key=?", (google_sub, device_key)
    ).fetchone()
    conn.close()
    return row["token"]


def touch_token(token):
    """Best-effort refresh of last_seen_at for whichever device/OAuth
    token this is. Called on the successful-auth path, so it's
    rate-limited: the write only happens when the stored last_seen_at is
    already more than LAST_SEEN_REFRESH_SECONDS old. A no-op for the
    shared mcp_users token and the owner token (neither has a
    last_seen_at column / row). Never raises: a failed touch must not
    fail the request it's decorating."""
    now = time.time()
    cutoff = now - LAST_SEEN_REFRESH_SECONDS
    try:
        conn = _connect()
        conn.execute(
            "UPDATE device_tokens SET last_seen_at=? "
            "WHERE token=? AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (now, token, cutoff),
        )
        conn.execute(
            "UPDATE oauth_tokens SET last_seen_at=? "
            "WHERE token=? AND (last_seen_at IS NULL OR last_seen_at < ?)",
            (now, token, cutoff),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def list_tokens(google_sub, current_token=None):
    """Every active sign-in/session token for this account, newest
    first: the per-device sign-in tokens (device_tokens) and the
    connector sessions (oauth_tokens), as one unified list. Each entry:
    {token_id, label, created_at, last_seen_at, is_current, kind}. Never
    returns a raw token. is_current marks the row matching current_token
    (the token the caller is holding right now), so the UI can label
    "This device" and refuse to let it revoke itself by accident.

    The shared mcp_users token is intentionally NOT listed: it has no
    per-device identity to show, and revoking it belongs to the
    account-wide control, not this list."""
    current_id = _token_id(current_token) if current_token else None
    conn = _connect()
    device_rows = conn.execute(
        "SELECT token_id, label, created_at, last_seen_at FROM device_tokens "
        "WHERE google_sub=? ORDER BY created_at DESC",
        (google_sub,),
    ).fetchall()
    oauth_rows = conn.execute(
        "SELECT token_id, label, client_name, created_at, last_seen_at FROM oauth_tokens "
        "WHERE google_sub=? ORDER BY created_at DESC",
        (google_sub,),
    ).fetchall()
    conn.close()

    out = []
    for r in device_rows:
        out.append(
            {
                "token_id": r["token_id"],
                "label": r["label"] or UNKNOWN_DEVICE,
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
                "is_current": current_id is not None and r["token_id"] == current_id,
                "kind": "device",
            }
        )
    for r in oauth_rows:
        # A pre-metadata oauth_tokens row has no token_id; fall back to a
        # freshly derived label so it's still shown (and revocable by the
        # account-wide control), just unlabelled.
        label = r["label"] or (f"{r['client_name']} (connector)" if r["client_name"] else UNKNOWN_DEVICE)
        out.append(
            {
                "token_id": r["token_id"],
                "label": label,
                "created_at": r["created_at"],
                "last_seen_at": r["last_seen_at"],
                "is_current": current_id is not None and r["token_id"] is not None and r["token_id"] == current_id,
                "kind": "connector",
            }
        )
    return out


def revoke_token(google_sub, token_id):
    """Revoke exactly one device/session token by its public token_id,
    scoped to google_sub so a user can never revoke someone else's.
    Idempotent: revoking an already-gone or never-existed token_id is a
    silent no-op, not an error. Leaves every other token for this
    account (and every other account) untouched. Returns True if a row
    was actually deleted, False otherwise (useful for a 404-vs-200
    decision in the route, though the route treats both as success)."""
    if not token_id:
        return False
    conn = _connect()
    cur = conn.execute(
        "DELETE FROM device_tokens WHERE google_sub=? AND token_id=?", (google_sub, token_id)
    )
    deleted = cur.rowcount
    cur = conn.execute(
        "DELETE FROM oauth_tokens WHERE google_sub=? AND token_id=?", (google_sub, token_id)
    )
    deleted += cur.rowcount
    conn.commit()
    conn.close()
    return deleted > 0


# --- OAuth 2.1 + PKCE authorization server ------------------------------
# Backs the /oauth/* routes in server.py; see that module's docstring for
# the flow. A generic, spec-compliant implementation: any MCP client that
# does OAuth discovery (Dynamic Client Registration + authorization code +
# PKCE) can use this, not just one specific product.


def _sha256_hex(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _s256_challenge(code_verifier):
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


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
    conn = _connect()
    conn.execute(
        "INSERT INTO oauth_clients (client_id, client_name, redirect_uris, token_endpoint_auth_method, created_at) "
        "VALUES (?,?,?,?,?)",
        (client_id, client_name, json.dumps(redirect_uris), token_endpoint_auth_method or "none", time.time()),
    )
    conn.commit()
    conn.close()
    return client_id


def get_oauth_client(client_id):
    conn = _connect()
    row = conn.execute("SELECT * FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    client = dict(row)
    client["redirect_uris"] = json.loads(client["redirect_uris"])
    return client


def issue_oauth_code(client_id, google_sub, email, redirect_uri, code_challenge, resource, ttl_seconds=600):
    """Mints a one-time authorization code for a client whose redirect_uri
    has already been validated against its registration by the /oauth/authorize
    route. Returns the raw code (shown to the caller exactly once); only
    its hash is stored."""
    raw = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    conn.execute(
        "INSERT INTO oauth_codes "
        "(code_hash, client_id, google_sub, email, redirect_uri, code_challenge, resource, created_at, expires_at, consumed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,NULL)",
        (_sha256_hex(raw), client_id, google_sub, email, redirect_uri, code_challenge, resource, now, now + ttl_seconds),
    )
    conn.commit()
    conn.close()
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
    (google_sub, email) on success."""
    code_hash = _sha256_hex(code)
    conn = _connect()
    row = conn.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (code_hash,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("invalid authorization code")
    row = dict(row)
    now = time.time()
    if row["consumed_at"] is not None:
        conn.close()
        raise ValueError("authorization code already used")
    if row["expires_at"] < now:
        conn.close()
        raise ValueError("authorization code expired")
    if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
        conn.close()
        raise ValueError("client_id or redirect_uri does not match")
    if row["resource"].rstrip("/") != resource.rstrip("/"):
        conn.close()
        raise ValueError("resource does not match")
    if _s256_challenge(code_verifier) != row["code_challenge"]:
        conn.close()
        raise ValueError("PKCE verification failed")

    # The earlier `consumed_at is not None` check is only a fast-path
    # rejection. This UPDATE's `WHERE consumed_at IS NULL` is what
    # actually enforces single-use: two concurrent redemptions both pass
    # the read above, but only one UPDATE wins the row. The loser's
    # rowcount is 0, caught below.
    cursor = conn.execute(
        "UPDATE oauth_codes SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL", (now, code_hash)
    )
    if cursor.rowcount != 1:
        conn.close()
        raise ValueError("authorization code already used")
    conn.commit()
    conn.close()
    return row["google_sub"], row["email"]


def issue_install_code(bearer_token, ttl_seconds=local_setup.INSTALL_CODE_TTL_SECONDS):
    """Mints a one-time code for the /setup/install curl-able one-liner.
    The page hands this to a signed-in caller instead of embedding their
    real bearer token in plaintext (which would otherwise end up in shell
    history forever). Stores bearer_token itself rather than an identity
    to re-derive it from, so this works identically for a per-user token
    or the shared owner token. Returns the raw code (shown exactly once);
    only its hash is stored, same reasoning as issue_oauth_code."""
    raw = secrets.token_urlsafe(32)
    now = time.time()
    conn = _connect()
    conn.execute(
        "INSERT INTO install_codes (code_hash, bearer_token, created_at, expires_at, consumed_at) "
        "VALUES (?,?,?,?,NULL)",
        (_sha256_hex(raw), bearer_token, now, now + ttl_seconds),
    )
    conn.commit()
    conn.close()
    return raw


def redeem_install_code(code):
    """Validates and single-use-consumes an install code, same atomicity
    guarantee as redeem_oauth_code (the final UPDATE's WHERE consumed_at
    IS NULL is what actually enforces single-use under a race, not the
    earlier read). Raises ValueError with a caller-safe message. The
    install script surfaces this directly, so "expired" vs "already
    used" vs "invalid" are distinguished rather than collapsed into one
    generic failure. Returns the bearer token on success."""
    code_hash = _sha256_hex(code)
    conn = _connect()
    row = conn.execute("SELECT * FROM install_codes WHERE code_hash=?", (code_hash,)).fetchone()
    if row is None:
        conn.close()
        raise ValueError("invalid install code")
    row = dict(row)
    now = time.time()
    if row["consumed_at"] is not None:
        conn.close()
        raise ValueError("install code already used")
    if row["expires_at"] < now:
        conn.close()
        raise ValueError("install code expired")

    cursor = conn.execute(
        "UPDATE install_codes SET consumed_at=? WHERE code_hash=? AND consumed_at IS NULL", (now, code_hash)
    )
    if cursor.rowcount != 1:
        conn.close()
        raise ValueError("install code already used")
    conn.commit()
    conn.close()
    return row["bearer_token"]


def list_oauth_clients():
    """Admin visibility: every OAuth client that's ever completed Dynamic
    Client Registration. No client secret to redact (token_endpoint_auth_method
    is always "none": these are public clients, and PKCE is the actual
    protection), so this is safe to return in full."""
    conn = _connect()
    rows = conn.execute(
        "SELECT client_id, client_name, redirect_uris, token_endpoint_auth_method, created_at "
        "FROM oauth_clients ORDER BY created_at"
    ).fetchall()
    conn.close()
    clients = [dict(r) for r in rows]
    for c in clients:
        c["redirect_uris"] = json.loads(c["redirect_uris"])
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
    conn = _connect()
    conn.execute("DELETE FROM oauth_clients WHERE client_id=?", (client_id,))
    conn.execute("DELETE FROM oauth_codes WHERE client_id=? AND consumed_at IS NULL", (client_id,))
    conn.commit()
    conn.close()


def list_oauth_tokens():
    """Admin visibility: every access token issued via the OAuth flow.
    Never returns the token value itself, only enough to identify and
    revoke it (see revoke_oauth_token)."""
    conn = _connect()
    rows = conn.execute(
        "SELECT google_sub, email, client_name, created_at, "
        "substr(token, 1, 8) || '...' AS token_prefix FROM oauth_tokens ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke_oauth_token(token):
    """Deletes one OAuth-issued access token. The client that held it
    needs to redo the full authorization flow to get a new one. Doesn't
    touch mcp_users' own token (see revoke()), so this can't accidentally
    break a user's direct Claude Code config while disconnecting one
    Connector-style client."""
    conn = _connect()
    conn.execute("DELETE FROM oauth_tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()


def mint_oauth_token(google_sub, email, client_name, user_agent=None):
    """A fresh access token for one (user, OAuth client) pair. Deliberately
    not the same token get_or_create_token returns, so disconnecting this
    client later doesn't also invalidate the user's direct MCP client
    config. Always mints a new token, unlike get_or_create_token: a second
    OAuth authorization for the same client is a re-consent, not a replay,
    and each grant gets its own revocable credential.

    Records the per-session metadata (token_id, User-Agent-derived
    label, last_seen_at) so this grant shows up in list_tokens() next to
    the direct-sign-in devices. user_agent is optional and best-effort:
    the OAuth /oauth/token caller is a server (claude.ai's backend), so
    the label usually resolves via client_name, not the UA."""
    token = secrets.token_urlsafe(32)
    now = time.time()
    ua_label = label_for_user_agent(user_agent)
    label = ua_label if ua_label != UNKNOWN_DEVICE else (f"{client_name} (connector)" if client_name else UNKNOWN_DEVICE)
    conn = _connect()
    conn.execute(
        "INSERT INTO oauth_tokens (token, token_id, google_sub, email, client_name, label, created_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (token, _token_id(token), google_sub, email, client_name, label, now, now),
    )
    conn.commit()
    conn.close()
    return token
