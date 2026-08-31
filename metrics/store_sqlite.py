"""SQLite backend for the execution recorder, local dev only. Selected
via metrics/store.py's dispatcher; see store_dynamodb.py for the
deployed backend, needed because a container's local filesystem
doesn't persist across invocations. Same function signatures either
way; callers go through store.py and never know which backend is
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
from metrics.errors import SessionOwnershipError

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
        owner TEXT,
        source TEXT DEFAULT 'bedrock_agent',
        status TEXT DEFAULT 'closed'
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
        status TEXT,
        latency_ms INTEGER DEFAULT 0,
        timestamp REAL DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS context_blocks (
        session_id TEXT,
        seq INTEGER,
        category TEXT,
        label TEXT,
        char_count INTEGER,
        token_estimate INTEGER,
        turn_n INTEGER,
        status TEXT,
        content TEXT
    );
"""


_TURNS_MIGRATION_COLUMNS = ("cache_read_input_tokens", "cache_write_input_tokens")


def _migrate_turns_table(conn):
    """CREATE TABLE IF NOT EXISTS never alters an already-existing table.
    A turns table created before prompt caching was added has no
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
    """Same reasoning as _migrate_turns_table: a sessions table that
    predates per-owner data isolation has no `owner` column. Existing
    rows get owner=NULL, meaning "recorded before ownership existed",
    treated the same as the server owner's own sessions (visible with
    the admin/owner token, invisible to any per-user token's filtered
    view). source/status predate OTLP ingestion the same way; ALTER
    TABLE ... DEFAULT backfills pre-existing rows as
    source='bedrock_agent', status='closed', which is what
    record_session's atomic one-shot write always effectively was."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "owner" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN owner TEXT")
    if "source" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN source TEXT DEFAULT 'bedrock_agent'")
    if "status" not in existing:
        conn.execute("ALTER TABLE sessions ADD COLUMN status TEXT DEFAULT 'closed'")


_TOOL_CALLS_MIGRATION_COLUMNS = ("latency_ms", "timestamp")


def _migrate_tool_calls_table(conn):
    """Same reasoning as _migrate_turns_table: the Tool calls tab
    needs a per-call
    latency and timestamp that a pre-existing tool_calls table doesn't
    have. Both default to 0 for rows written before this column
    existed, meaning "unknown," not a real zero latency."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(tool_calls)")}
    for column in _TOOL_CALLS_MIGRATION_COLUMNS:
        if column not in existing:
            type_ = "REAL" if column == "timestamp" else "INTEGER"
            conn.execute(f"ALTER TABLE tool_calls ADD COLUMN {column} {type_} DEFAULT 0")


def _migrate_context_blocks_table(conn):
    """Same reasoning as _migrate_turns_table: a context_blocks table
    from before block content was captured has no `content` column.
    Existing rows get content=NULL, meaning "not captured for this
    block" (shown as such in the Context Explorer), not an empty
    string. Those are different things: an empty string is a
    genuinely blank block, NULL is "we never stored it"."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(context_blocks)")}
    if "content" not in existing:
        conn.execute("ALTER TABLE context_blocks ADD COLUMN content TEXT")


def _connect():
    # CREATE TABLE IF NOT EXISTS is cheap and idempotent, so every caller
    # (record AND every read) gets a guaranteed-initialized DB through
    # this one function, so reading before any session has ever been
    # recorded works instead of failing.
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    _migrate_turns_table(conn)
    _migrate_sessions_table(conn)
    _migrate_tool_calls_table(conn)
    _migrate_context_blocks_table(conn)
    conn.commit()
    return conn


def _session_owner(conn, session_id):
    row = conn.execute("SELECT owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    return row["owner"] if row else None


def _visible(session_owner, caller_owner):
    """caller_owner=None means "the admin/owner token", which sees
    everything. Otherwise a caller only sees sessions it owns."""
    return caller_owner is None or session_owner == caller_owner


def record_session(prompt, model_id, loop_result, owner=None):
    """loop_result is runtime.run_agent_loop()'s return dict. owner is
    the Google account `sub` that recorded this session, or None for
    the server owner's own (e.g. the local agent calling this directly,
    not through an authenticated MCP connection); see _visible(). This
    stays the one-shot atomic path (source='bedrock_agent', status
    'closed' immediately). OTLP ingestion uses start_or_get_session +
    append_* below instead, since that data arrives incrementally."""
    session_id = str(uuid.uuid4())
    cost = estimate_cost(model_id, loop_result["input_tokens"], loop_result["output_tokens"])
    ts = time.time()

    conn = _connect()
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            "bedrock_agent",
            "closed",
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
            "INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?)",
            (
                session_id,
                i,
                call["tool"],
                json.dumps(call["args"]),
                call["status"],
                call.get("latency_ms", 0),
                call.get("timestamp", ts),
            ),
        )
    for seq, block in enumerate(loop_result.get("context_blocks", [])):
        conn.execute(
            "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                seq,
                block["category"],
                block["label"],
                block["char_count"],
                block["token_estimate"],
                block.get("turn_n"),  # null for a pre-conversation block
                block.get("status"),
                block.get("content"),
            ),
        )
    conn.commit()
    conn.close()
    return session_id


def get_session_metrics(session_id, owner=None):
    """Session metadata and per-prompt processing metrics as separate
    sub-dicts. owner=None (the admin/owner token) sees any session; a
    per-user owner only sees sessions it owns; everything else reads as
    "not found," the same shape as a genuinely missing session_id, so
    this can't be used to probe which session_ids exist."""
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
            "source": row["source"] or "bedrock_agent",
            "status": row["status"] or "closed",
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
            "SELECT tool_name, status, COUNT(*) as calls FROM tool_calls WHERE session_id=? GROUP BY tool_name, status",
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
            "SELECT tool_name, status, COUNT(*) as calls FROM tool_calls GROUP BY tool_name, status"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_agent_trace(session_id, owner=None):
    conn = _connect()
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        return []
    rows = conn.execute(
        "SELECT tool_name, args, status, latency_ms, timestamp FROM tool_calls WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    conn.close()
    return [
        {
            "tool": r["tool_name"],
            "args": json.loads(r["args"]),
            "status": r["status"],
            "latency_ms": r["latency_ms"] or 0,
            "timestamp": r["timestamp"] or 0,
        }
        for r in rows
    ]


def get_cost_estimate(session_id=None, period_seconds=None, owner=None):
    conn = _connect()
    if session_id:
        row = conn.execute("SELECT estimated_cost, owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        conn.close()
        if not row or not _visible(row["owner"], owner):
            return 0.0  # tool contract is `-> float`; "no such session" has no cost
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
    cumulative_pct. token_estimate is a character-based estimate (see
    agent/runtime.py), not exact Bedrock usage, so cumulative_pct is
    illustrative too."""
    conn = _connect()
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        return []
    rows = conn.execute(
        "SELECT category, label, char_count, token_estimate, turn_n, status, content FROM context_blocks "
        "WHERE session_id=? ORDER BY seq",
        (session_id,),
    ).fetchall()
    conn.close()
    return build_timeline(dict(r) for r in rows)


def get_recent_sessions(limit=10, owner=None, include_test_sessions=False):
    """Aggregates (turn_count, cache tokens, tool call/error counts) are
    correlated subqueries, not per-row follow-up fetches, so one query
    total, not the N+1 pattern the dashboard's KPI strip comment
    explicitly calls out avoiding elsewhere.

    include_test_sessions=False (the default) excludes rows whose
    session_id starts with "api-tests-", the live black-box suite in
    api_tests/ posts real OTLP payloads against a real deployment and
    otherwise clutters every account's dashboard with synthetic probe
    sessions (see api_tests/client.py's session_id generation). The
    prefix is the marker, not a schema column, since every api_tests
    session_id is already generated with it."""
    conn = _connect()
    agg_sql = (
        "(SELECT COUNT(*) FROM turns WHERE turns.session_id = sessions.session_id) AS turn_count, "
        "(SELECT COALESCE(SUM(cache_read_input_tokens), 0) FROM turns "
        "WHERE turns.session_id = sessions.session_id) AS cache_read_tokens, "
        "(SELECT COALESCE(SUM(input_tokens), 0) FROM turns "
        "WHERE turns.session_id = sessions.session_id) AS fresh_input_tokens, "
        "(SELECT COUNT(*) FROM tool_calls WHERE tool_calls.session_id = sessions.session_id) AS tool_call_total, "
        "(SELECT COUNT(*) FROM tool_calls WHERE tool_calls.session_id = sessions.session_id "
        "AND tool_calls.status = 'error') AS tool_call_errors"
    )
    where_clauses = []
    params = []
    if owner is not None:
        where_clauses.append("owner=?")
        params.append(owner)
    if not include_test_sessions:
        where_clauses.append("session_id NOT LIKE 'api-tests-%'")
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT session_id, prompt, model, total_tokens, estimated_cost, timestamp, "
        f"source, status, {agg_sql} FROM sessions {where_sql} ORDER BY timestamp DESC LIMIT ?",
        params,
    ).fetchall()
    conn.close()
    return [{**dict(r), "source": r["source"] or "bedrock_agent", "status": r["status"] or "closed"} for r in rows]


def start_or_get_session(session_id, owner=None, source="claude_code", model=None):
    """Entry point for OTLP ingestion. Unlike record_session (one atomic
    insert once a Bedrock agent's loop finishes), a live Claude Code/
    Copilot session reports turns/tool-calls/context-blocks one at a
    time as they happen. Idempotent on session_id: a client that
    reconnects or retries a batch calls this again with the same ID and
    gets the existing session back rather than a duplicate row. The
    session_id is caller-supplied (Claude Code's/Copilot's own ID), not
    a server-generated uuid4 like record_session's, so a caller must
    not be able to "adopt" another owner's session_id by reusing it;
    raises SessionOwnershipError when it already belongs to someone
    else."""
    conn = _connect()
    row = conn.execute("SELECT session_id, owner FROM sessions WHERE session_id=?", (session_id,)).fetchone()
    if row:
        if not _visible(row["owner"], owner):
            conn.close()
            raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")
    else:
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, None, model, 0, 0, 0, 0, 0, 0.0, time.time(), owner, source, "open"),
        )
        conn.commit()
    conn.close()
    return session_id


def _check_ownership_or_raise(conn, session_id, owner):
    """Every append_*/close_session call must not be able to write into
    a session_id owned by someone else, same reasoning as
    start_or_get_session's docstring. A session_id with no matching row
    yet (append called before start_or_get_session, shouldn't normally
    happen) is treated as belonging to no one, i.e. deny-by-default for
    any non-admin caller."""
    if not _visible(_session_owner(conn, session_id), owner):
        conn.close()
        raise SessionOwnershipError(f"session_id {session_id!r} belongs to a different owner")


def _next_seq(conn, table, session_id, seq_column="seq"):
    row = conn.execute(
        f"SELECT COALESCE(MAX({seq_column}), -1) + 1 as n FROM {table} WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return row["n"]


def _recost_session(conn, session_id):
    """Re-derive estimated_cost from the session's own model + running
    token totals. Called after every append_turn, since OTLP sessions
    never get a single final loop_result to price in one shot the way
    record_session does."""
    row = conn.execute(
        "SELECT model, input_tokens, output_tokens FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    cost = estimate_cost(row["model"], row["input_tokens"], row["output_tokens"])
    conn.execute("UPDATE sessions SET estimated_cost=? WHERE session_id=?", (cost, session_id))


def append_turn(session_id, turn_data, owner=None):
    """turn_data: {input_tokens, output_tokens, latency_ms,
    cache_read_input_tokens=0, cache_write_input_tokens=0}. Adds a turns
    row and folds its totals into the parent session row so
    get_session_metrics/get_recent_sessions stay accurate without a
    separate aggregation pass. owner must match the session's own owner
    (see _check_ownership_or_raise): a per-user caller can't write into
    a session_id it doesn't own."""
    conn = _connect()
    _check_ownership_or_raise(conn, session_id, owner)
    turn_n = _next_seq(conn, "turns", session_id, seq_column="turn_n")
    conn.execute(
        "INSERT INTO turns VALUES (?,?,?,?,?,?,?)",
        (
            session_id,
            turn_n,
            turn_data["input_tokens"],
            turn_data["output_tokens"],
            turn_data["latency_ms"],
            turn_data.get("cache_read_input_tokens", 0),
            turn_data.get("cache_write_input_tokens", 0),
        ),
    )
    conn.execute(
        "UPDATE sessions SET input_tokens=input_tokens+?, output_tokens=output_tokens+?, "
        "total_tokens=total_tokens+?, latency_ms=latency_ms+? WHERE session_id=?",
        (
            turn_data["input_tokens"],
            turn_data["output_tokens"],
            turn_data["input_tokens"] + turn_data["output_tokens"],
            turn_data["latency_ms"],
            session_id,
        ),
    )
    _recost_session(conn, session_id)
    conn.commit()
    conn.close()


def append_tool_call(session_id, tool_call, owner=None):
    """tool_call: {tool, args, status, latency_ms=0, timestamp=None
    (defaults to now)}. owner must match the session's own owner (see
    _check_ownership_or_raise)."""
    conn = _connect()
    _check_ownership_or_raise(conn, session_id, owner)
    seq = _next_seq(conn, "tool_calls", session_id)
    conn.execute(
        "INSERT INTO tool_calls VALUES (?,?,?,?,?,?,?)",
        (
            session_id,
            seq,
            tool_call["tool"],
            json.dumps(tool_call["args"]),
            tool_call["status"],
            tool_call.get("latency_ms", 0),
            tool_call.get("timestamp") or time.time(),
        ),
    )
    conn.execute("UPDATE sessions SET tool_call_count=tool_call_count+1 WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()


_PROMPT_PREVIEW_CHARS = 200


def append_context_block(session_id, block, owner=None):
    """block: {category, label, char_count, token_estimate, turn_n,
    status=None, content=None}, the same shape record_session's
    context_blocks rows use. owner must match the session's own owner
    (see _check_ownership_or_raise).

    OTLP sessions are created by start_or_get_session with prompt=None,
    since a live session's content arrives incrementally as blocks. The
    first user-authored block backfills sessions.prompt here. The
    `prompt IS NULL` guard means a later turn's user message can never
    clobber turn 0's original prompt. Truncated for list-view display;
    the full text already lives in this block's own
    context_blocks.content row."""
    conn = _connect()
    _check_ownership_or_raise(conn, session_id, owner)
    seq = _next_seq(conn, "context_blocks", session_id)
    conn.execute(
        "INSERT INTO context_blocks VALUES (?,?,?,?,?,?,?,?,?)",
        (
            session_id,
            seq,
            block["category"],
            block["label"],
            block["char_count"],
            block["token_estimate"],
            block["turn_n"],
            block.get("status"),
            block.get("content"),
        ),
    )
    if block["category"] == "user":
        preview = (block.get("content") or block.get("label") or "")[:_PROMPT_PREVIEW_CHARS]
        conn.execute(
            "UPDATE sessions SET prompt=? WHERE session_id=? AND (prompt IS NULL OR prompt='')",
            (preview, session_id),
        )
    conn.commit()
    conn.close()


def close_session(session_id, final_totals=None, owner=None):
    """Marks a session complete once the client process exits or goes
    idle past some timeout. final_totals, if given, overwrites the
    incrementally-accumulated totals with exact values (e.g. from the
    client's own final usage report): {input_tokens, output_tokens,
    total_tokens, latency_ms}. Optional: the dashboard already tolerates
    an 'open' session showing partial data, so a session that never
    gets explicitly closed (e.g. the client crashed) just stays 'open'
    forever rather than blocking anything. owner must match the
    session's own owner (see _check_ownership_or_raise)."""
    conn = _connect()
    _check_ownership_or_raise(conn, session_id, owner)
    if final_totals:
        conn.execute(
            "UPDATE sessions SET input_tokens=?, output_tokens=?, total_tokens=?, latency_ms=? WHERE session_id=?",
            (
                final_totals["input_tokens"],
                final_totals["output_tokens"],
                final_totals["total_tokens"],
                final_totals["latency_ms"],
                session_id,
            ),
        )
        _recost_session(conn, session_id)
    conn.execute("UPDATE sessions SET status='closed' WHERE session_id=?", (session_id,))
    conn.commit()
    conn.close()
