"""Regression tests for metrics/store_firestore.py, run against the real
Firestore emulator (FIRESTORE_EMULATOR_HOST), not a fake/mock, since
Firestore's transaction semantics (used for start_or_get_session's
create-or-fetch idempotency and the append_*/close_session sequence-
number allocation) are exactly the behavior under test and are
impractical to fake convincingly.

Skipped whole-module if the emulator isn't reachable, so this doesn't
break CI/local runs without `gcloud emulators firestore start` (or the
equivalent) running. Start one locally with:

    gcloud emulators firestore start --host-port=localhost:8080

and export FIRESTORE_EMULATOR_HOST=localhost:8080 before running pytest.

Covers the same correctness contracts as test_metrics_store_dynamodb.py
(owner-scoping, ownership-error raising, aggregate filtering) plus the
Firestore-specific "(no prompt)" backfill fix in append_context_block.
"""

import os
import socket

import pytest

from metrics.errors import SessionOwnershipError


def _emulator_reachable():
    host_port = os.environ.get("FIRESTORE_EMULATOR_HOST")
    if not host_port:
        return False
    host, _, port = host_port.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _emulator_reachable(),
    reason="FIRESTORE_EMULATOR_HOST not set or emulator not reachable; skipping live Firestore tests",
)


def _basic_loop_result(**overrides):
    result = {
        "text": "answer",
        "trace": [{"tool": "list_services", "args": {}, "status": "ok"}],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }
    result.update(overrides)
    return result


# --- empty-DB reads: no crash, empty/None results ---------------------


def test_get_session_metrics_missing_returns_none(isolated_firestore_db):
    assert isolated_firestore_db.get_session_metrics("nope") is None


def test_empty_reads_return_empty_not_crash(isolated_firestore_db):
    store = isolated_firestore_db
    assert store.get_token_breakdown("nope") == []
    assert store.get_agent_trace("nope") == []
    assert store.get_context_timeline("nope") == []
    assert store.get_tool_metrics("nope") == []
    assert store.get_cost_estimate("nope") == 0.0
    assert store.get_recent_sessions() == []
    assert store.get_tool_metrics() == []
    assert store.get_cost_estimate() == 0.0


# --- record_session round trip -----------------------------------------


def test_record_session_round_trip(isolated_firestore_db):
    store = isolated_firestore_db
    loop_result = {
        "text": "answer",
        "trace": [{"tool": "list_services", "args": {"x": 1}, "status": "ok", "latency_ms": 20}],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
        "context_blocks": [
            {"category": "system", "label": "sys", "char_count": 40, "token_estimate": 10, "turn_n": None},
            {"category": "user", "label": "prompt", "char_count": 20, "token_estimate": 5, "turn_n": 0},
        ],
    }
    session_id = store.record_session("why is x degraded?", "us.anthropic.claude-sonnet-4-6", loop_result)

    metrics = store.get_session_metrics(session_id)
    assert metrics["prompt_metrics"]["prompt"] == "why is x degraded?"
    assert metrics["prompt_metrics"]["input_tokens"] == 10
    assert metrics["session"]["status"] == "closed"
    assert metrics["session"]["source"] == "bedrock_agent"

    breakdown = store.get_token_breakdown(session_id)
    assert [b["turn_n"] for b in breakdown] == [0]

    trace = store.get_agent_trace(session_id)
    assert trace == [
        {
            "tool": "list_services",
            "args": {"x": 1},
            "status": "ok",
            "latency_ms": 20,
            "timestamp": trace[0]["timestamp"],
        }
    ]

    timeline = store.get_context_timeline(session_id)
    assert [b["category"] for b in timeline] == ["system", "user"]
    assert timeline[-1]["cumulative_tokens"] == 15


def test_record_session_without_context_blocks_key_does_not_crash(isolated_firestore_db):
    store = isolated_firestore_db
    session_id = store.record_session("q", "m", _basic_loop_result())
    assert store.get_context_timeline(session_id) == []


# --- start_or_get_session idempotency and cross-owner rejection --------


def test_start_or_get_session_is_idempotent(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")
    store.append_turn(sid, {"input_tokens": 100, "output_tokens": 20, "latency_ms": 500})
    store.start_or_get_session("otel-1", owner="u1", source="claude_code", model="claude-sonnet-4-5")

    assert len(store.get_recent_sessions(owner="u1")) == 1
    metrics = store.get_session_metrics("otel-1", owner="u1")
    assert metrics["prompt_metrics"]["input_tokens"] == 100


def test_start_or_get_session_cross_owner_raises(isolated_firestore_db):
    store = isolated_firestore_db
    store.start_or_get_session("otel-shared", owner="alice", source="claude_code")
    with pytest.raises(SessionOwnershipError):
        store.start_or_get_session("otel-shared", owner="bob")


def test_start_or_get_session_admin_can_adopt_any_existing(isolated_firestore_db):
    store = isolated_firestore_db
    store.start_or_get_session("otel-admin", owner="alice", source="claude_code")
    # owner=None is the admin/owner token, and must not raise.
    result = store.start_or_get_session("otel-admin", owner=None)
    assert result == "otel-admin"


# --- append_*/close_session ownership checks ---------------------------


def test_append_turn_rejects_non_owner(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-2", owner="alice", source="claude_code")
    with pytest.raises(SessionOwnershipError):
        store.append_turn(sid, {"input_tokens": 1, "output_tokens": 1, "latency_ms": 1}, owner="bob")


def test_append_tool_call_rejects_non_owner(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-3", owner="alice", source="claude_code")
    with pytest.raises(SessionOwnershipError):
        store.append_tool_call(sid, {"tool": "Read", "args": {}, "status": "ok"}, owner="bob")


def test_append_context_block_rejects_non_owner(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-4", owner="alice", source="claude_code")
    with pytest.raises(SessionOwnershipError):
        store.append_context_block(
            sid,
            {"category": "system", "label": "x", "char_count": 1, "token_estimate": 1, "turn_n": 0},
            owner="bob",
        )


def test_close_session_rejects_non_owner(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-5", owner="alice", source="claude_code")
    with pytest.raises(SessionOwnershipError):
        store.close_session(sid, owner="bob")


def test_append_functions_deny_by_default_for_unknown_session(isolated_firestore_db):
    """A session_id with no matching doc yet is treated as belonging to
    no one: deny-by-default for any non-admin caller."""
    store = isolated_firestore_db
    with pytest.raises(SessionOwnershipError):
        store.append_turn("never-started", {"input_tokens": 1, "output_tokens": 1, "latency_ms": 1}, owner="alice")


# --- owner filtering correctness ----------------------------------------


def test_owner_none_sees_everything(isolated_firestore_db):
    store = isolated_firestore_db
    store.record_session("q1", "m", _basic_loop_result(), owner="alice-sub")
    store.record_session("q2", "m", _basic_loop_result(), owner="bob-sub")
    store.record_session("q3", "m", _basic_loop_result())
    assert len(store.get_recent_sessions(limit=10)) == 3


def test_owner_only_sees_own_sessions(isolated_firestore_db):
    store = isolated_firestore_db
    store.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")
    store.record_session("bob's q", "m", _basic_loop_result(), owner="bob-sub")

    alice_sessions = store.get_recent_sessions(limit=10, owner="alice-sub")
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["prompt"] == "alice's q"


def test_owner_cannot_read_another_owners_session_by_id(isolated_firestore_db):
    store = isolated_firestore_db
    session_id = store.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")

    assert store.get_session_metrics(session_id, owner="bob-sub") is None
    assert store.get_token_breakdown(session_id, owner="bob-sub") == []
    assert store.get_agent_trace(session_id, owner="bob-sub") == []
    assert store.get_context_timeline(session_id, owner="bob-sub") == []
    assert store.get_tool_metrics(session_id, owner="bob-sub") == []
    assert store.get_cost_estimate(session_id, owner="bob-sub") == 0.0

    assert store.get_session_metrics(session_id, owner="alice-sub") is not None
    assert store.get_session_metrics(session_id, owner=None) is not None


def test_owner_aggregate_cost_and_tool_metrics_filtered(isolated_firestore_db):
    store = isolated_firestore_db
    store.record_session("alice's q", "m", _basic_loop_result(), owner="alice-sub")
    store.record_session("bob's q", "m", _basic_loop_result(), owner="bob-sub")

    alice_cost = store.get_cost_estimate(owner="alice-sub")
    total_cost = store.get_cost_estimate()
    assert 0 < alice_cost < total_cost

    alice_tools = store.get_tool_metrics(owner="alice-sub")
    assert len(alice_tools) == 1
    assert alice_tools[0]["calls"] == 1

    all_tools = store.get_tool_metrics()
    assert all_tools[0]["calls"] == 2


def test_get_recent_sessions_owner_filtering(isolated_firestore_db):
    store = isolated_firestore_db
    for i in range(3):
        store.record_session(f"alice q{i}", "m", _basic_loop_result(), owner="alice-sub")
    for i in range(2):
        store.record_session(f"bob q{i}", "m", _basic_loop_result(), owner="bob-sub")

    alice_sessions = store.get_recent_sessions(limit=10, owner="alice-sub")
    assert len(alice_sessions) == 3
    assert all(s["prompt"].startswith("alice") for s in alice_sessions)

    everyone = store.get_recent_sessions(limit=10)
    assert len(everyone) == 5


def test_owner_defaults_to_none_backward_compatible(isolated_firestore_db):
    store = isolated_firestore_db
    session_id = store.record_session("q", "m", _basic_loop_result())
    assert store.get_session_metrics(session_id) is not None
    assert len(store.get_recent_sessions()) == 1


# --- append_turn totals / cost recompute --------------------------------


def test_append_turn_accumulates_session_totals(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-accum", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
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


def test_append_tool_call_increments_count(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-tools", source="claude_code")
    store.append_tool_call(sid, {"tool": "Read", "args": {"path": "x"}, "status": "success"})
    store.append_tool_call(sid, {"tool": "Edit", "args": {}, "status": "success"})

    assert store.get_session_metrics(sid)["prompt_metrics"]["tool_call_count"] == 2
    trace = store.get_agent_trace(sid)
    assert [t["tool"] for t in trace] == ["Read", "Edit"]


def test_append_context_block_orders_by_arrival(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-ctx", source="claude_code")
    store.append_context_block(
        sid, {"category": "system", "label": "system prompt", "char_count": 400, "token_estimate": 100, "turn_n": 0}
    )
    store.append_context_block(
        sid, {"category": "user", "label": "user turn", "char_count": 40, "token_estimate": 10, "turn_n": 0}
    )

    timeline = store.get_context_timeline(sid)
    assert [b["category"] for b in timeline] == ["system", "user"]
    assert timeline[-1]["cumulative_tokens"] == 110


def test_close_session_marks_status_and_applies_final_totals(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-close", source="claude_code", model="us.anthropic.claude-sonnet-4-6")
    store.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    assert store.get_session_metrics(sid)["session"]["status"] == "open"

    store.close_session(
        sid,
        final_totals={
            "input_tokens": 999,
            "output_tokens": 111,
            "total_tokens": 1110,
            "latency_ms": 5000,
        },
    )

    metrics = store.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 999
    assert metrics["prompt_metrics"]["total_tokens"] == 1110


def test_close_session_without_final_totals_just_closes(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-close2", source="claude_code")
    store.append_turn(sid, {"input_tokens": 10, "output_tokens": 5, "latency_ms": 50})
    store.close_session(sid)

    metrics = store.get_session_metrics(sid)
    assert metrics["session"]["status"] == "closed"
    assert metrics["prompt_metrics"]["input_tokens"] == 10


# --- prompt-backfill fix -------------------------------------------------


def test_first_user_context_block_backfills_prompt(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-prompt", source="claude_code")
    assert store.get_session_metrics(sid)["prompt_metrics"]["prompt"] is None

    store.append_context_block(
        sid,
        {
            "category": "user",
            "label": "first message",
            "char_count": 20,
            "token_estimate": 5,
            "turn_n": 0,
            "content": "what is degraded right now?",
        },
    )
    assert store.get_session_metrics(sid)["prompt_metrics"]["prompt"] == "what is degraded right now?"


def test_second_user_context_block_does_not_overwrite_prompt(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-prompt2", source="claude_code")
    store.append_context_block(
        sid,
        {
            "category": "user",
            "label": "first",
            "char_count": 10,
            "token_estimate": 3,
            "turn_n": 0,
            "content": "first user message",
        },
    )
    store.append_context_block(
        sid,
        {
            "category": "user",
            "label": "second",
            "char_count": 10,
            "token_estimate": 3,
            "turn_n": 1,
            "content": "a much later second user message",
        },
    )
    assert store.get_session_metrics(sid)["prompt_metrics"]["prompt"] == "first user message"


def test_prompt_backfill_truncates_long_content(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-prompt3", source="claude_code")
    long_text = "x" * 500
    store.append_context_block(
        sid,
        {
            "category": "user",
            "label": "long",
            "char_count": 500,
            "token_estimate": 125,
            "turn_n": 0,
            "content": long_text,
        },
    )
    prompt = store.get_session_metrics(sid)["prompt_metrics"]["prompt"]
    assert len(prompt) <= 200


def test_non_user_context_block_does_not_set_prompt(isolated_firestore_db):
    store = isolated_firestore_db
    sid = store.start_or_get_session("otel-prompt4", source="claude_code")
    store.append_context_block(
        sid,
        {
            "category": "system",
            "label": "sys",
            "char_count": 10,
            "token_estimate": 3,
            "turn_n": None,
            "content": "system prompt text",
        },
    )
    assert store.get_session_metrics(sid)["prompt_metrics"]["prompt"] is None
