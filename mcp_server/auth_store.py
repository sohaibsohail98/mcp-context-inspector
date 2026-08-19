"""Per-user MCP token store — separate from metrics/ (session-execution
data) since this is identity/credential data with a different lifecycle
and sensitivity. SQLite only for now, same "local dev" starting point
as metrics/store_sqlite.py — needs the same kind of swap to a
persistent backend before this server runs somewhere with an ephemeral
filesystem (e.g. Cloud Run without a mounted volume).

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
    returns the same token, not a new one.

    A single atomic upsert, not a SELECT-then-INSERT — two concurrent
    first-time sign-ins for the same brand-new google_sub (e.g. a friend
    double-clicking "Sign in with Google," or two server worker
    processes handling near-simultaneous requests) would otherwise both
    pass the "no existing row" check before either commits, and the
    second INSERT would crash on the google_sub PRIMARY KEY constraint.
    ON CONFLICT DO UPDATE makes the loser of the race a no-op update
    instead of a crash, and the final SELECT always returns whichever
    token actually won, so this function can't fail EVER just because it
    was called concurrently for the same account."""
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
    conn = _connect()
    row = conn.execute("SELECT 1 FROM mcp_users WHERE token=?", (token,)).fetchone()
    conn.close()
    return row is not None


def get_sub_for_token(token):
    """The google_sub that owns this per-user token — used to attribute
    data (record/filter by owner) to whoever's actually connected, not
    just to check "is this token valid at all." Returns None if the
    token doesn't belong to any signed-in user (e.g. it's the owner
    token, or invalid)."""
    conn = _connect()
    row = conn.execute("SELECT google_sub FROM mcp_users WHERE token=?", (token,)).fetchone()
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
