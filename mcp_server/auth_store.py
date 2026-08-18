"""Per-user MCP token store — separate from metrics/ (session-execution
data) since this is identity/credential data with a different lifecycle
and sensitivity. SQLite only for now, matching metrics/store_sqlite.py's
"local dev" starting point (see docs/PROJECT.md's Storage section in the
sre-investigation-agent repo for the precedent) — needs the same kind of
swap to a persistent backend before this server runs somewhere with an
ephemeral filesystem (e.g. Cloud Run without a mounted volume).

One row per Google account (`google_sub`, the stable, non-reassignable
subject identifier from the verified ID token — never the email, which
a user can change). A user who signs in again gets their existing token
back rather than a fresh one, so pasting the same "Sign in with Google"
flow twice doesn't invalidate a token they've already put in an MCP
client config.
"""

import secrets
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "mcp_auth.db"

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS mcp_users (
        google_sub TEXT PRIMARY KEY,
        email TEXT,
        token TEXT UNIQUE,
        created_at REAL
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
    returns the same token, not a new one."""
    conn = _connect()
    row = conn.execute(
        "SELECT token FROM mcp_users WHERE google_sub=?", (google_sub,)
    ).fetchone()
    if row:
        # Email can legitimately change (a Google account's email is
        # mutable); keep it current without touching the token.
        conn.execute("UPDATE mcp_users SET email=? WHERE google_sub=?", (email, google_sub))
        conn.commit()
        conn.close()
        return row["token"]

    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO mcp_users VALUES (?,?,?,?)",
        (google_sub, email, token, time.time()),
    )
    conn.commit()
    conn.close()
    return token


def is_valid_token(token):
    conn = _connect()
    row = conn.execute("SELECT 1 FROM mcp_users WHERE token=?", (token,)).fetchone()
    conn.close()
    return row is not None


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
