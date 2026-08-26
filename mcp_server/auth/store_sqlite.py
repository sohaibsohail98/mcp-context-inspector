"""SQLite backend for the per-user MCP token store — the local-dev /
default backend, same role as metrics/store_sqlite.py. Reached only
through auth/store.py's dispatcher (STORAGE_BACKEND env var); import
this module directly only for the DB_PATH test hook (see
tests/conftest.py's isolated_auth_store fixture) or in local dev where
STORAGE_BACKEND is unset. See auth/store_dynamodb.py for the persistent
backend meant for Cloud Run's ephemeral filesystem — this module's
writes are lost on cold start / reset across concurrent instances.

Separate from metrics/ (session-execution data) since this is
identity/credential data with a different lifecycle and sensitivity.

One row per Google account (`google_sub`, the stable, non-reassignable
subject identifier from the verified ID token — never the email, which
a user can change). A user who signs in again gets their existing token
back rather than a fresh one, so pasting the same "Sign in with Google"
flow twice doesn't invalidate a token they've already put in an MCP
client config.

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

DB_PATH = Path(__file__).parent.parent / "data" / "mcp_auth.db"

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS mcp_users (
        google_sub TEXT PRIMARY KEY,
        email TEXT,
        token TEXT UNIQUE,
        created_at REAL
    );

    -- OAuth clients registered via POST /oauth/register (RFC 7591 Dynamic
    -- Client Registration) — e.g. claude.ai registers itself here once,
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
    -- stored — same reasoning as hashing a password: a code is a bearer
    -- secret, and this table being readable shouldn't hand out usable
    -- codes. SHA-256 (not a slow KDF like bcrypt) is enough here since
    -- the code is a 256-bit random secret, not a low-entropy password —
    -- matches how this project already treats bearer tokens elsewhere.
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
        created_at REAL
    );

    -- One-time install codes: the short-lived code /setup/install's
    -- curl-able one-liner exchanges for the caller's real bearer token,
    -- so the token itself never sits in shell history via a plaintext
    -- curl arg. Deliberately a separate, simpler table from oauth_codes
    -- above — no client_id/redirect_uri/PKCE, since there's no OAuth
    -- client or browser redirect involved here, just "the signed-in
    -- page handed you a code, now exchange it." Same single-use +
    -- TTL + hash-keyed-storage shape as oauth_codes for the same
    -- reasoning (see issue_install_code/redeem_install_code below).
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
    conn.commit()
    return conn


def get_or_create_token(google_sub, email):
    """Returns this Google account's MCP token, minting one on first
    sign-in. Idempotent per google_sub — re-running the sign-in flow
    returns the same token, not a new one.

    A single atomic upsert, not a SELECT-then-INSERT — two concurrent
    first-time sign-ins for the same brand-new google_sub (e.g. a user
    double-clicking "Sign in with Google," or two server worker
    processes handling near-simultaneous requests) would otherwise both
    pass the "no existing row" check before either commits, and the
    second INSERT would crash on the google_sub PRIMARY KEY constraint.
    ON CONFLICT DO UPDATE makes the loser of the race a no-op update
    instead of a crash, and the final SELECT always returns whichever
    token actually won."""
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
    """True for either a direct Google-sign-in token (mcp_users) or a
    token minted through the OAuth flow for a Connector-style client
    (oauth_tokens) — both are equally valid bearer credentials from the
    caller's perspective; only their provenance differs."""
    conn = _connect()
    row = conn.execute(
        "SELECT 1 FROM mcp_users WHERE token=? UNION SELECT 1 FROM oauth_tokens WHERE token=?",
        (token, token),
    ).fetchone()
    conn.close()
    return row is not None


def get_sub_for_token(token):
    """The google_sub that owns this token — used to attribute data
    (record/filter by owner) to whoever's actually connected, not just to
    check "is this token valid at all." Checks both mcp_users and
    oauth_tokens (see is_valid_token). Returns None if the token doesn't
    belong to any signed-in user (e.g. it's the owner token, or invalid)."""
    conn = _connect()
    row = conn.execute("SELECT google_sub FROM mcp_users WHERE token=?", (token,)).fetchone()
    if row is None:
        row = conn.execute("SELECT google_sub FROM oauth_tokens WHERE token=?", (token,)).fetchone()
    conn.close()
    return row["google_sub"] if row else None


def list_users():
    """Admin visibility — who has ever signed in. Never returns tokens
    themselves, only enough to identify an account for revocation."""
    conn = _connect()
    rows = conn.execute(
        "SELECT google_sub, email, created_at FROM mcp_users ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revoke(google_sub):
    """Deletes a user's token — they'd need to sign in again to get a
    new one. No graceful in-place rotation; revocation is intentionally
    blunt for a personal-scale token store."""
    conn = _connect()
    conn.execute("DELETE FROM mcp_users WHERE google_sub=?", (google_sub,))
    conn.commit()
    conn.close()


# --- OAuth 2.1 + PKCE authorization server ------------------------------
# Backs the /oauth/* routes in server.py — see that module's docstring for
# the flow. A generic, spec-compliant implementation: any MCP client that
# does OAuth discovery (Dynamic Client Registration + authorization code +
# PKCE) can use this, not just one specific product.


def _sha256_hex(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _s256_challenge(code_verifier):
    digest = hashlib.sha256(code_verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def register_oauth_client(redirect_uris, client_name=None, token_endpoint_auth_method="none"):
    """Dynamic Client Registration (RFC 7591) — mints a fresh client_id for
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
        # code — and transitively, via /oauth/token, the resulting access
        # token — travel in cleartext to anyone on-path between the
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
    (this is what stops an attacker who intercepts the code — without the
    verifier, which never left the original requester — from redeeming
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
    # the read above, but only one UPDATE wins the row — the loser's
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
    """Mints a one-time code for the /setup/install curl-able one-liner —
    the page hands this to a signed-in caller instead of embedding their
    real bearer token in plaintext (which would otherwise end up in shell
    history forever). Stores bearer_token itself (not an identity to
    re-derive it from later) — simplest correct contract, and works
    identically for a per-user token or the shared owner token, neither
    of which needs re-deriving. Returns the raw code (shown exactly
    once); only its hash is stored, same reasoning as issue_oauth_code."""
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
    earlier read). Raises ValueError with a caller-safe message — the
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
    """Admin visibility — every OAuth client that's ever completed Dynamic
    Client Registration. No client secret to redact (token_endpoint_auth_method
    is always "none" — these are public clients, PKCE is the actual
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
    invalidate access tokens this client already obtained — oauth_tokens
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
    """Admin visibility — every access token issued via the OAuth flow.
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
    """Deletes one OAuth-issued access token — the client that held it
    needs to redo the full authorization flow to get a new one. Doesn't
    touch mcp_users' own token (see revoke()), so this can't accidentally
    break a user's direct Claude Code config while disconnecting one
    Connector-style client."""
    conn = _connect()
    conn.execute("DELETE FROM oauth_tokens WHERE token=?", (token,))
    conn.commit()
    conn.close()


def mint_oauth_token(google_sub, email, client_name):
    """A fresh access token for one (user, OAuth client) pair — deliberately
    not the same token get_or_create_token returns, so disconnecting this
    client later doesn't also invalidate the user's direct MCP client
    config. Always mints a new token, unlike get_or_create_token: a second
    OAuth authorization for the same client is a re-consent, not a replay,
    and each grant gets its own revocable credential."""
    token = secrets.token_urlsafe(32)
    conn = _connect()
    conn.execute(
        "INSERT INTO oauth_tokens (token, google_sub, email, client_name, created_at) VALUES (?,?,?,?,?)",
        (token, google_sub, email, client_name, time.time()),
    )
    conn.commit()
    conn.close()
    return token
