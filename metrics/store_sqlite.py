"""SQLite backend for the execution recorder — local dev only. Selected
via metrics/store.py's dispatcher; see store_dynamodb.py for the
deployed backend, needed because a container's local filesystem
doesn't persist across invocations. Same function signatures either
way — callers go through store.py and never know which backend is
active.
"""

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from mci_common.pricing import estimate_cost
from mci_common.timeline import build_timeline

# Overridable so a deployed container can point writes at an ephemeral
# path (e.g. /tmp, on a read-only or baked-in image layer) instead of
# the local dev default under the repo's data/ dir.
DB_PATH = Path(os.environ.get("METRICS_DB_PATH", Path(__file__).parent.parent / "data" / "metrics.db"))


_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        prompt TEXT,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        total_tokens INTEGER,
        latency_ms INTEGER,
        tool_call_count INTEGER,
        estimated_cost REAL,
        timestamp REAL,
        owner TEXT
    );
    CREATE TABLE IF NOT EXISTS turns (
        session_id TEXT,
        turn_n INTEGER,
        input_tokens INTEGER,
        output_tokens INTEGER,
        latency_ms INTEGER,
        cache_read_input_tokens INTEGER,
        cache_write_input_tokens INTEGER
    );
    CREATE TABLE IF NOT EXISTS tool_calls (
        session_id TEXT,
        seq INTEGER,
        tool_name TEXT,
        args TEXT,
        status TEXT
    );
    CREATE TABLE IF NOT EXISTS context_blocks (
        session_id TEXT,
        seq INTEGER,
        category TEXT,
        label TEXT,
        char_count INTEGER,
        token_estimate INTEGER,
        turn_n INTEGER,
        status TEXT
    );
"""


_TURNS_MIGRATION_COLUMNS = ("cache_read_input_tokens", "cache_write_input_tokens")


def _migrate_turns_table(conn):
    """CREATE TABLE IF NOT EXISTS never alters an already-existing table —
    a turns table created before prompt caching was added has no
    cache_read_input_tokens/cache_write_input_tokens columns, and every
    read/write against them then crashes with "no such column" on that
    pre-existing data/metrics.db. ALTER TABLE ADD COLUMN (idempotent via
    the PRAGMA check) is the one-time fix-up so existing local DBs don't
    need manual deletion to pick up a schema change."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(turns)")}
    for column in _TURNS_MIGRATION_COLUMNS:
        if column not in existing:
            conn.execute(f"ALTER TABLE turns ADD COLUMN {column} INTEGER DEFAULT 0")


def _migrate_sessions_table(conn):
    """Same reasoning as _migrate_turns_table — a sessions table that
    predates per-owner data isolation has no `owner` column. Existing
    rows get owner=NULL, meaning "recorded before ownership existed" —
    treated the same as the server owner's own sessions (visible with
    the admin/owner token, invisible to any per-user token's filtered
    view)."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "owner" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT")


def _connect():
    # CREATE TABLE IF NOT EXISTS is cheap and idempotent, so every caller
    # (record AND every read) gets a guaranteed-initialized DB through
    # this one function — reading before any session has ever been
    # recorded works instead of failing.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_turns_table(conn)
    _migrate_sessions_table(conn)
    conn.commit()
    return conn


def _session_owner(conn, session_id):
    row = conn.execute("SELECT owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["owner"] if row else None


def _visible(session_owner, caller_owner):
    """caller_owner=None means "the admin/owner token" — sees
    everything. Otherwise a caller only sees sessions it owns."""
    return caller_owner is None or session_owner == caller_owner


def record_session(prompt, model_id, loop_result, owner=None):
    """loop_result is runtime.run_agent_loop()'s return dict. owner is
    the Google account `sub` that recorded this session, or None for
    the server owner's own (e.g. the local agent calling this directly,
    not through an authenticated MCP connection) — see _visible()."""
    session_id = str(uuid.uuid4())
    cost = estimate_cost(model_id, loop_result["input_tokens"], loop_result["output_tokens"])
    ts = time.time()

    conn = _connect()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            prompt,
            model_id,
            loop_result["input_tokens"],
            loop_result["output_tokens"],
            loop_result["total_tokens"],
            loop_result["latency_ms"],
            len(loop_result["trace"]),
            cost,
            ts,
            owner,
        ),
    )
    for i, turn in enumerate(loop_result["turns"]):
        conn.execute(
            "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                i,
                turn["input_tokens"],
                turn["output_tokens"],
                turn["latency_ms"],
                turn.get("cache_read_input_tokens", 0),
                turn.get("cache_write_input_tokens", 0),
            ),
        )
    for i, call in enumerate(loop_result["trace"]):
        conn.execute(
            "INSERT INTO tool_calls VALUES (?,?,?,?,?)",
            (session_id, i, call["tool"], json.dumps(call["args"]), call["status"]),
        )
    for seq, block in enumerate(loop_result.get("context_blocks", [])):
        conn.execute(
            "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?)",
            (
                session_id,
                seq,
                block["category"],
                block["label"],
                block["char_count"],
                block["token_estimate"],
                block["turn_n"],
                block.get("status"),
            ),
        )
    conn.commit()
    conn.close()
    return session_id


def get_session_metrics(session_id, owner=None):
    """Session metadata split from per-prompt processing metrics — the two
    are conceptually different (identity/timing vs. what it cost to
    answer this prompt), so callers get them as separate sub-dicts rather
    than one flat blob. owner=None (the admin/owner token) sees any
    session; a per-user owner only sees sessions it owns — everything
    else reads as "not found," the same shape as a genuinely missing
    session_id, so this can't be used to probe which session_ids exist."""
    conn = _connect()
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    conn.close()
    if not row:
        return None
    row = dict(row)
    if not _visible(row["owner"], owner):
        return None
    return {
        "session": {
            "session_id": row["session_id"],
            "model": row["model"],
            "timestamp": row["timestamp"],
        },
        "prompt_metrics": {
            "prompt": row["prompt"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "total_tokens": row["total_tokens"],
            "latency_ms": row["latency_ms"],
            "tool_call_count": row["tool_call_count"],
            "estimated_cost": row["estimated_cost"],
        },
    }


def get_token_breakdown(session_id, owner=None):
    conn = _connect()
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        return []
    rows = conn.execute(
        "SELECT turn_n, input_tokens, output_tokens, latency_ms, "
        "cache_read_input_tokens, cache_write_input_tokens FROM turns "
        "WHERE session_id=? ORDER BY turn_n",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_tool_metrics(session_id=None, owner=None):
    conn = _connect()
    if session_id:
        if not _visible(_session_owner(conn, session_id), owner):
            conn.close()
            return []
        rows = conn.execute(
            "SELECT tool_name, status, COUNT(*) as calls FROM tool_calls "
            "WHERE session_id=? GROUP BY tool_name, status",
            (session_id,),
        ).fetchall()
    elif owner is not None:
        rows = conn.execute(
            "SELECT tool_name, status, COUNT(*) as calls FROM tool_calls "
            "WHERE session_id IN (SELECT session_id FROM sessions WHERE owner=?) "
            "GROUP BY tool_name, status",
            (owner,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT tool_name, status, COUNT(*) as calls FROM tool_calls "
            "GROUP BY tool_name, status"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agent_trace(session_id, owner=None):
    conn = _connect()
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        return []
    rows = conn.execute(
        "SELECT tool_name, args, status FROM tool_calls WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    conn.close()
    return [
        {"tool": r["tool_name"], "args": json.loads(r["args"]), "status": r["status"]}
        for r in rows
    ]


def get_cost_estimate(session_id=None, period_seconds=None, owner=None):
    conn = _connect()
    if session_id:
        row = conn.execute(
            "SELECT estimated_cost, owner FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        conn.close()
        if not row or not _visible(row["owner"], owner):
            return None
        return row["estimated_cost"]

    since = time.time() - period_seconds if period_seconds else 0
    if owner is not None:
        row = conn.execute(
            "SELECT SUM(estimated_cost) as total FROM sessions WHERE timestamp >= ? AND owner=?",
            (since, owner),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT SUM(estimated_cost) as total FROM sessions WHERE timestamp >= ?", (since,)
        ).fetchone()
    conn.close()
    return row["total"] or 0.0


def get_context_timeline(session_id, owner=None):
    """Ordered, categorized breakdown of everything that entered this
    session's context window, with a running cumulative_tokens/
    cumulative_pct — token_estimate is a character-based estimate (see
    agent/runtime.py), not exact Bedrock usage, so cumulative_pct is
    illustrative too."""
    conn = _connect()
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        return []
    rows = conn.execute(
        "SELECT category, label, char_count, token_estimate, turn_n, status FROM context_blocks "
        "WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    conn.close()
    return build_timeline(dict(r) for r in rows)


def get_recent_sessions(limit=10, owner=None):
    conn = _connect()
    if owner is not None:
        rows = conn.execute(
            "SELECT session_id, prompt, model, total_tokens, estimated_cost, timestamp "
            "FROM sessions WHERE owner=? ORDER BY timestamp DESC LIMIT ?",
            (owner, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT session_id, prompt, model, total_tokens, estimated_cost, timestamp "
            "FROM sessions ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
