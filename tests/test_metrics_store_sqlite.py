"""Regression tests for store_sqlite.py — pins the schema-init-on-read
behavior in _connect()."""

import sqlite3


def test_pre_caching_db_migrates_instead_of_crashing(isolated_sqlite_db):
    """The actual bug: CREATE TABLE IF NOT EXISTS never alters a table
    that already exists. A turns table created before prompt caching was
    added (no cache_read/write columns) must not crash reads/writes with
    "no such column" — this is exactly the shape data/metrics.db was in
    for anyone who ran the agent before this change, and _connect() must
    migrate it on first touch, not require manually deleting the file."""
    store = isolated_sqlite_db

    # Build the table in its pre-caching shape, bypassing _connect() so
    # the migration path is genuinely exercised, not skipped by CREATE
    # TABLE IF NOT EXISTS already having the new columns.
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "CREATE TABLE turns (session_id TEXT, turn_n INTEGER, input_tokens INTEGER, "
        "output_tokens INTEGER, latency_ms INTEGER)"
    )
    conn.execute("INSERT INTO turns VALUES ('s1', 0, 100, 20, 500)")
    conn.commit()
    conn.close()

    breakdown = store.get_token_breakdown("s1")
    assert breakdown == [
        {
            "turn_n": 0,
            "input_tokens": 100,
            "output_tokens": 20,
            "latency_ms": 500,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
    ]


def test_reads_on_empty_db_do_not_crash(isolated_sqlite_db):
    """The actual bug: a fresh/missing DB file must not 500 on read."""
    store = isolated_sqlite_db
    assert store.get_recent_sessions() == []
    assert store.get_session_metrics("nonexistent") is None
    assert store.get_token_breakdown("nonexistent") == []
    assert store.get_tool_metrics() == []
    assert store.get_agent_trace("nonexistent") == []
    assert store.get_cost_estimate() == 0.0


def _fake_loop_result(**overrides):
    result = {
        "text": "the answer",
        "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
        "turns": [{"input_tokens": 100, "output_tokens": 20, "latency_ms": 500}],
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
        "latency_ms": 500,
    }
    result.update(overrides)
    return result


def test_record_then_read_round_trip(isolated_sqlite_db):
    store = isolated_sqlite_db
    session_id = store.record_session("why is x degraded?", "us.anthropic.claude-sonnet-4-6", _fake_loop_result())

    metrics = store.get_session_metrics(session_id)
    assert metrics["session"]["session_id"] == session_id
    assert metrics["prompt_metrics"]["prompt"] == "why is x degraded?"
    assert metrics["prompt_metrics"]["total_tokens"] == 120
    assert metrics["prompt_metrics"]["tool_call_count"] == 1

    assert store.get_token_breakdown(session_id) == [
        {
            "turn_n": 0,
            "input_tokens": 100,
            "output_tokens": 20,
            "latency_ms": 500,
            "cache_read_input_tokens": 0,
            "cache_write_input_tokens": 0,
        }
    ]
    assert store.get_agent_trace(session_id) == [
        {"tool": "list_services", "args": {}, "status": "ok"}
    ]
    recent = store.get_recent_sessions(limit=5)
    assert len(recent) == 1
    assert recent[0]["session_id"] == session_id


def test_get_session_metrics_shape_has_no_backend_internals(isolated_sqlite_db):
    """SQLite has no partition-key concept to leak, but pin the expected
    session/prompt_metrics split anyway — this is the contract
    store_dynamodb.py must match (it previously leaked an internal "sk"
    field flat in the response; see test_metrics_store_dynamodb.py)."""
    store = isolated_sqlite_db
    session_id = store.record_session("q", "us.anthropic.claude-sonnet-4-6", _fake_loop_result())
    metrics = store.get_session_metrics(session_id)
    assert set(metrics.keys()) == {"session", "prompt_metrics"}
    assert set(metrics["session"].keys()) == {"session_id", "model", "timestamp"}
    assert set(metrics["prompt_metrics"].keys()) == {
        "prompt", "input_tokens", "output_tokens",
        "total_tokens", "latency_ms", "tool_call_count", "estimated_cost",
    }


def test_turns_carry_cache_token_fields(isolated_sqlite_db):
    """Prompt-caching fields (§6a) — must round-trip even when the loop
    result predates caching support (turn dicts without the keys), via
    the .get(..., 0) default in record_session."""
    store = isolated_sqlite_db
    session_id = store.record_session(
        "q",
        "us.anthropic.claude-sonnet-4-6",
        _fake_loop_result(
            turns=[
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "latency_ms": 500,
                    "cache_read_input_tokens": 80,
                    "cache_write_input_tokens": 0,
                }
            ]
        ),
    )
    breakdown = store.get_token_breakdown(session_id)
    assert breakdown[0]["cache_read_input_tokens"] == 80
    assert breakdown[0]["cache_write_input_tokens"] == 0


def test_record_session_without_context_blocks_key_does_not_crash(isolated_sqlite_db):
    """loop_result predating context_blocks (e.g. an older caller, or a
    test fixture like _fake_loop_result above) must not KeyError."""
    store = isolated_sqlite_db
    session_id = store.record_session("q", "us.anthropic.claude-sonnet-4-6", _fake_loop_result())
    assert store.get_context_timeline(session_id) == []


def test_context_timeline_round_trip_and_cumulative_math(isolated_sqlite_db):
    store = isolated_sqlite_db
    session_id = store.record_session(
        "q",
        "us.anthropic.claude-sonnet-4-6",
        _fake_loop_result(
            context_blocks=[
                {"category": "system", "label": "System prompt", "char_count": 400, "token_estimate": 100, "turn_n": None},
                {"category": "user", "label": "User prompt", "char_count": 40, "token_estimate": 10, "turn_n": 0},
                {"category": "tool_result", "label": "Tool result: list_services", "char_count": 200, "token_estimate": 50, "turn_n": 0, "status": "error"},
            ]
        ),
    )
    timeline = store.get_context_timeline(session_id)
    assert [b["category"] for b in timeline] == ["system", "user", "tool_result"]
    assert [b["cumulative_tokens"] for b in timeline] == [100, 110, 160]
    assert timeline[0]["turn_n"] is None
    assert timeline[2]["status"] == "error"
    assert timeline[0]["status"] is None
    # 160 / 200_000 * 100
    assert timeline[2]["cumulative_pct"] == 0.08
