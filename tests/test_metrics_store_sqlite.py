"""Regression tests for store_sqlite.py — pins the schema-init-on-read
behavior in _connect()."""

import sqlite3


def test_pre_caching_db_migrates_instead_of_crashing(isolated_sqlite_db):
    """CREATE TABLE IF NOT EXISTS never alters a table that already
    exists. A turns table created before prompt caching was added (no
    cache_read/write columns) must not crash reads/writes with "no such
    column" — _connect() must migrate it on first touch, not require
    manually deleting the file."""
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
    """A fresh/missing DB file must not 500 on read."""
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
    trace = store.get_agent_trace(session_id)
    assert len(trace) == 1
    assert trace[0]["tool"] == "list_services"
    assert trace[0]["args"] == {}
    assert trace[0]["status"] == "ok"
    assert trace[0]["latency_ms"] == 0
    assert trace[0]["timestamp"] > 0
    recent = store.get_recent_sessions(limit=5)
    assert len(recent) == 1
    assert recent[0]["session_id"] == session_id


def test_get_session_metrics_shape_has_no_backend_internals(isolated_sqlite_db):
    """SQLite has no partition-key concept to leak, but pin the expected
    session/prompt_metrics split anyway — this is the contract
    store_dynamodb.py must also match (see test_metrics_store_dynamodb.py)."""
    store = isolated_sqlite_db
    session_id = store.record_session("q", "us.anthropic.claude-sonnet-4-6", _fake_loop_result())
    metrics = store.get_session_metrics(session_id)
    assert set(metrics.keys()) == {"session", "prompt_metrics"}
    assert set(metrics["session"].keys()) == {"session_id", "model", "timestamp", "source", "status"}
    assert set(metrics["prompt_metrics"].keys()) == {
        "prompt", "input_tokens", "output_tokens",
        "total_tokens", "latency_ms", "tool_call_count", "estimated_cost",
    }


def test_turns_carry_cache_token_fields(isolated_sqlite_db):
    """Prompt-caching fields must round-trip even when the loop result
    predates caching support (turn dicts without the keys), via the
    .get(..., 0) default in record_session."""
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


def test_owner_none_sees_everything(isolated_sqlite_db):
    """owner=None is the admin/owner-token view — must see sessions
    regardless of who recorded them."""
    store = isolated_sqlite_db
    store.record_session("q1", "m", _fake_loop_result(), owner="alice-sub")
    store.record_session("q2", "m", _fake_loop_result(), owner="bob-sub")
    store.record_session("q3", "m", _fake_loop_result())  # owner=None too
    assert len(store.get_recent_sessions(limit=10)) == 3


def test_owner_only_sees_own_sessions(isolated_sqlite_db):
    store = isolated_sqlite_db
    store.record_session("alice's q", "m", _fake_loop_result(), owner="alice-sub")
    store.record_session("bob's q", "m", _fake_loop_result(), owner="bob-sub")

    alice_sessions = store.get_recent_sessions(limit=10, owner="alice-sub")
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["prompt"] == "alice's q"


def test_owner_cannot_read_another_owners_session_by_id(isolated_sqlite_db):
    """Guessing/leaking a session_id must not bypass ownership — reads
    as "not found," identical to a genuinely nonexistent session_id."""
    store = isolated_sqlite_db
    session_id = store.record_session("alice's q", "m", _fake_loop_result(), owner="alice-sub")

    assert store.get_session_metrics(session_id, owner="bob-sub") is None
    assert store.get_token_breakdown(session_id, owner="bob-sub") == []
    assert store.get_agent_trace(session_id, owner="bob-sub") == []
    assert store.get_context_timeline(session_id, owner="bob-sub") == []
    assert store.get_tool_metrics(session_id, owner="bob-sub") == []
    assert store.get_cost_estimate(session_id, owner="bob-sub") is None

    # The actual owner, and the admin (owner=None), both still can.
    assert store.get_session_metrics(session_id, owner="alice-sub") is not None
    assert store.get_session_metrics(session_id, owner=None) is not None


def test_owner_aggregate_cost_and_tool_metrics_filtered(isolated_sqlite_db):
    store = isolated_sqlite_db
    store.record_session("alice's q", "m", _fake_loop_result(), owner="alice-sub")
    store.record_session("bob's q", "m", _fake_loop_result(), owner="bob-sub")

    alice_cost = store.get_cost_estimate(owner="alice-sub")
    total_cost = store.get_cost_estimate()
    assert 0 < alice_cost < total_cost

    alice_tools = store.get_tool_metrics(owner="alice-sub")
    assert len(alice_tools) == 1  # list_services, from alice's one session
    all_tools = store.get_tool_metrics()
    assert all_tools[0]["calls"] == 2  # both sessions used list_services once each


def test_owner_defaults_to_none_backward_compatible(isolated_sqlite_db):
    """Every owner param defaults to None (admin view) — existing callers
    that never pass owner keep working exactly as before this feature."""
    store = isolated_sqlite_db
    session_id = store.record_session("q", "m", _fake_loop_result())
    assert store.get_session_metrics(session_id) is not None
    assert len(store.get_recent_sessions()) == 1


def test_metrics_db_path_env_override(tmp_path, monkeypatch):
    """A deployed container sets METRICS_DB_PATH to point writes at an
    ephemeral path instead of the local dev default — DB_PATH must pick
    that up at import time."""
    import importlib

    from metrics import store_sqlite

    override = tmp_path / "override.db"
    monkeypatch.setenv("METRICS_DB_PATH", str(override))
    try:
        importlib.reload(store_sqlite)
        assert store_sqlite.DB_PATH == override
        store_sqlite.record_session("q", "m", _fake_loop_result())
        assert override.exists()
    finally:
        monkeypatch.delenv("METRICS_DB_PATH", raising=False)
        importlib.reload(store_sqlite)


def test_start_or_get_session_is_idempotent(isolated_sqlite_db):
    """OTLP payloads can arrive out of order or get retried by the
    client — calling start_or_get_session twice for the same session_id
    must not create a second row or reset any totals already appended."""
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")
    store.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})
    store.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")

    assert len(store.get_recent_sessions(owner="u1")) == 1
    metrics = store.get_session_metrics("otel-1", owner="u1")
    assert metrics["prompt_metrics"]["input_tokens"] == 100


def test_append_turn_accumulates_session_totals(isolated_sqlite_db):
    """A live Claude Code session reports turns one at a time — the
    parent session row's totals must reflect the running sum, not just
    the most recently appended turn."""
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-2", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
    store.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})
    store.append_turn(sid, {"input_tokens": 50, "output_tokens": 10, "latency_ms": 300})

    metrics = store.get_session_metrics(sid)["prompt_metrics"]
    assert metrics["input_tokens"] == 150
    assert metrics["output_tokens"] == 30
    assert metrics["total_tokens"] == 180
    assert metrics["latency_ms"] == 800
    assert metrics["estimated_cost"] > 0

    breakdown = store.get_token_breakdown(sid)
    assert [t["turn_n"] for t in breakdown] == [0, 1]


def test_append_tool_call_increments_count(isolated_sqlite_db):
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-3", source="claude_code")
    store.append_tool_call(sid, {"tool": "Read", "args": {"path": "x"}, "status": "success"})
    store.append_tool_call(sid, {"tool": "Edit", "args": {}, "status": "success"})

    assert store.get_session_metrics(sid)["prompt_metrics"]["tool_call_count"] == 2
    trace = store.get_agent_trace(sid)
    assert [t["tool"] for t in trace] == ["Read", "Edit"]


def test_append_context_block_orders_by_arrival(isolated_sqlite_db):
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-4", source="claude_code")
    store.append_context_block(
        sid, {"category": "system", "label": "system prompt", "char_count": 400, "token_estimate": 100, "turn_n": 0}
    )
    store.append_context_block(
        sid, {"category": "user", "label": "user turn", "char_count": 40, "token_estimate": 10, "turn_n": 0}
    )

    timeline = store.get_context_timeline(sid)
    assert [b["category"] for b in timeline] == ["system", "user"]
    assert timeline[-1]["cumulative_tokens"] == 110


def test_append_context_block_round_trips_content(isolated_sqlite_db):
    """A block's raw text (used by the Context Explorer's expand-to-view
    feature) must round-trip through storage; a block that never sets
    `content` reads back as None ("not captured"), not a crash or an
    empty string — those mean different things."""
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-content", source="claude_code")
    store.append_context_block(
        sid,
        {
            "category": "system", "label": "System prompt", "char_count": 20,
            "token_estimate": 5, "turn_n": None, "content": "You are a helpful assistant.",
        },
    )
    store.append_context_block(
        sid, {"category": "user", "label": "User message", "char_count": 40, "token_estimate": 10, "turn_n": 0}
    )

    timeline = store.get_context_timeline(sid)
    assert timeline[0]["content"] == "You are a helpful assistant."
    assert timeline[1]["content"] is None


def test_close_session_marks_status_and_applies_final_totals(isolated_sqlite_db):
    """close_session's final_totals overwrite the incrementally-summed
    values with the client's own exact final report, when given."""
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-5", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
    store.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    assert store.get_session_metrics(sid)["session"]["status"] == "open"

    store.close_session(sid, final_totals={
        "input_tokens": 999, "output_tokens": 111, "total_tokens": 1110, "latency_ms": 5000,
    })

    metrics = store.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 999
    assert metrics["prompt_metrics"]["total_tokens"] == 1110


def test_close_session_without_final_totals_just_closes(isolated_sqlite_db):
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-6", source="claude_code")
    store.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    store.close_session(sid)

    metrics = store.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 10


def test_recent_sessions_carries_source_and_status(isolated_sqlite_db):
    """Dashboard session list needs a per-row source badge and pressure
    signal — both bedrock_agent (legacy) and OTLP-sourced sessions must
    carry these fields the same way."""
    store = isolated_sqlite_db
    store.record_session("q", "m", _fake_loop_result())
    store.start_or_get_session("otel-7", source="copilot")

    recent = {s["session_id"]: s for s in store.get_recent_sessions()}
    sources = {s["source"] for s in recent.values()}
    assert sources == {"bedrock_agent", "copilot"}
    assert all("status" in s for s in recent.values())


def test_recent_sessions_carries_turn_count(isolated_sqlite_db):
    """The session-list row needs "N turns" without a per-row follow-up
    fetch — get_recent_sessions must return the count directly."""
    store = isolated_sqlite_db
    three_turns = _fake_loop_result(
        turns=[
            {"input_tokens": 10, "output_tokens": 5, "latency_ms": 100},
            {"input_tokens": 20, "output_tokens": 5, "latency_ms": 100},
            {"input_tokens": 30, "output_tokens": 5, "latency_ms": 100},
        ]
    )
    three_turn_id = store.record_session("q", "m", three_turns)
    one_turn_id = store.record_session("q2", "m", _fake_loop_result())

    recent = {s["session_id"]: s for s in store.get_recent_sessions()}
    assert recent[three_turn_id]["turn_count"] == 3
    assert recent[one_turn_id]["turn_count"] == 1


def test_recent_sessions_carries_cache_and_tool_error_aggregates(isolated_sqlite_db):
    """KPI strip needs cache-hit-rate/tool-error-rate inputs in the same
    bulk fetch as everything else, no per-session follow-up."""
    store = isolated_sqlite_db
    session_id = store.start_or_get_session("sess-1", source="claude_code")
    store.append_turn(session_id, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 10, "cache_read_input_tokens": 300})
    store.append_turn(session_id, {"input_tokens": 50, "output_tokens": 10, "latency_ms": 10, "cache_read_input_tokens": 0})
    store.append_tool_call(session_id, {"tool": "a", "args": {}, "status": "success"})
    store.append_tool_call(session_id, {"tool": "b", "args": {}, "status": "error"})
    store.append_tool_call(session_id, {"tool": "c", "args": {}, "status": "error"})

    recent = {s["session_id"]: s for s in store.get_recent_sessions()}
    row = recent[session_id]
    assert row["cache_read_tokens"] == 300
    assert row["fresh_input_tokens"] == 150
    assert row["tool_call_total"] == 3
    assert row["tool_call_errors"] == 2


def test_pre_latency_tool_calls_table_migrates(isolated_sqlite_db):
    """A tool_calls table created before the Tool calls tab needed
    latency_ms/timestamp (see docs/internal/OTLP_INTEGRATION_PLAN.md's dashboard
    spec) has neither column — must migrate instead of crashing reads."""
    store = isolated_sqlite_db
    conn = sqlite3.connect(store.DB_PATH)
    conn.execute(
        "CREATE TABLE tool_calls (session_id TEXT, seq INTEGER, tool_name TEXT, args TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO tool_calls VALUES ('s1', 0, 'list_services', '{}', 'ok')")
    conn.commit()
    conn.close()

    trace = store.get_agent_trace("s1")
    assert trace == [
        {"tool": "list_services", "args": {}, "status": "ok", "latency_ms": 0, "timestamp": 0}
    ]


def test_append_tool_call_carries_latency_and_timestamp(isolated_sqlite_db):
    store = isolated_sqlite_db
    sid = store.start_or_get_session("otel-8", source="claude_code")
    store.append_tool_call(sid, {"tool": "Read", "args": {}, "status": "success", "latency_ms": 340})

    trace = store.get_agent_trace(sid)
    assert trace[0]["latency_ms"] == 340
    assert trace[0]["timestamp"] > 0
