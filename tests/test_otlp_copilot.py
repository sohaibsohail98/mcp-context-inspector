"""Tests for mcp_server/otlp/copilot.py.

Self-consistent fixture tests only: no real captured GitHub Copilot OTLP
payload exists to validate against (Copilot's OTel export is genuinely
unverified end-to-end — see the module docstring in
mcp_server/otlp/copilot.py and docs/internal/OTLP_INTEGRATION_PLAN.md's "###
GitHub Copilot" section for what is/isn't confirmed). These tests
hand-construct OTLP JSON span payloads matching copilot.py's own
documented attribute-name/shape assumptions and assert the mapper wires
them into metrics/store.py's schema correctly and idempotently — they
pin down *this repo's* mapping logic, not Copilot's real wire format.
"""

import json

from mcp_server.otlp import copilot


def _attr(key, value):
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    raise TypeError(f"unsupported attr value type: {type(value)}")


def _invoke_agent_span(trace_id, span_id):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "name": "invoke_agent",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "5000000000",
        "attributes": [],
    }


def _chat_span(trace_id, span_id, parent_span_id, input_messages, output_messages,
                input_tokens=100, output_tokens=20, model="gpt-4o"):
    return {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": "chat",
        "startTimeUnixNano": "1000000000",
        "endTimeUnixNano": "1500000000",
        "attributes": [
            _attr("gen_ai.input.messages", json.dumps(input_messages)),
            _attr("gen_ai.output.messages", json.dumps(output_messages)),
            _attr("gen_ai.usage.input_tokens", input_tokens),
            _attr("gen_ai.usage.output_tokens", output_tokens),
            _attr("gen_ai.request.model", model),
        ],
    }


def _execute_tool_span(trace_id, span_id, parent_span_id, tool_name, args, errored=False):
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": "execute_tool",
        "startTimeUnixNano": "1600000000",
        "endTimeUnixNano": "1700000000",
        "attributes": [
            _attr("gen_ai.tool.name", tool_name),
            _attr("gen_ai.tool.call.arguments", json.dumps(args)),
        ],
    }
    if errored:
        span["status"] = {"code": 2}
    return span


def test_invoke_agent_and_chat_span_produce_session_with_turn_and_tokens(isolated_sqlite_db):
    """A minimal invoke_agent + chat span pair should create a session
    (source='copilot'), record one turn with the chat span's own
    gen_ai.usage.* token counts, and store context blocks for the user
    input and assistant output messages."""
    store = isolated_sqlite_db
    trace_id = "trace-1"
    spans = [
        _invoke_agent_span(trace_id, "span-agent"),
        _chat_span(
            trace_id,
            "span-chat",
            "span-agent",
            input_messages=[{"role": "user", "content": "hello there"}],
            output_messages=[{"role": "assistant", "content": "hi, how can I help?"}],
            input_tokens=100,
            output_tokens=20,
        ),
    ]

    copilot.handle_traces({}, spans, owner=None)

    metrics = store.get_session_metrics(trace_id)
    assert metrics is not None
    assert metrics["session"]["source"] == "copilot"
    assert metrics["session"]["model"] == "gpt-4o"

    breakdown = store.get_token_breakdown(trace_id)
    assert len(breakdown) == 1
    assert breakdown[0]["input_tokens"] == 100
    assert breakdown[0]["output_tokens"] == 20
    assert breakdown[0]["latency_ms"] == 500  # (1.5e9 - 1e9) ns -> 500ms

    timeline = store.get_context_timeline(trace_id)
    categories = [b["category"] for b in timeline]
    assert "user" in categories
    assert "answer" in categories


def test_second_chat_span_repeating_history_does_not_duplicate_blocks(isolated_sqlite_db):
    """gen_ai.input.messages may carry the full cumulative conversation
    rather than just new messages each turn (unverified for Copilot —
    same defensive assumption as the Claude Code mapper). A second chat
    span whose input.messages repeats the first turn's messages plus one
    new one should only append the new tail, not re-append the repeats."""
    store = isolated_sqlite_db
    trace_id = "trace-2"
    turn1_user = {"role": "user", "content": "what's the weather"}
    turn1_answer = {"role": "assistant", "content": "sunny today"}
    turn2_user = {"role": "user", "content": "and tomorrow?"}
    turn2_answer = {"role": "assistant", "content": "rainy tomorrow"}

    spans_turn1 = [
        _invoke_agent_span(trace_id, "span-agent"),
        _chat_span(trace_id, "span-chat-1", "span-agent", [turn1_user], [turn1_answer]),
    ]
    copilot.handle_traces({}, spans_turn1, owner=None)
    timeline_after_turn1 = store.get_context_timeline(trace_id)
    assert len(timeline_after_turn1) == 2

    spans_turn2 = [
        _chat_span(
            trace_id,
            "span-chat-2",
            "span-agent",
            input_messages=[turn1_user, turn1_answer, turn2_user],
            output_messages=[turn2_answer],
        ),
    ]
    copilot.handle_traces({}, spans_turn2, owner=None)

    timeline_after_turn2 = store.get_context_timeline(trace_id)
    # Only the two genuinely new blocks (turn2_user, turn2_answer) should
    # have been appended on top of the first turn's two blocks.
    assert len(timeline_after_turn2) == 4
    assert [b["turn_n"] for b in timeline_after_turn2] == [0, 0, 1, 1]


def test_execute_tool_span_appends_tool_call_with_status(isolated_sqlite_db):
    """An execute_tool span (including MCP-sourced tool calls per the
    plan) should map to append_tool_call with the right tool name, args,
    and status — success by default, error when the span's OTLP status
    code is ERROR (2)."""
    store = isolated_sqlite_db
    trace_id = "trace-3"
    spans = [
        _invoke_agent_span(trace_id, "span-agent"),
        _execute_tool_span(
            trace_id, "span-tool-ok", "span-agent", "read_file", {"path": "foo.py"}
        ),
        _execute_tool_span(
            trace_id, "span-tool-err", "span-agent", "run_tests", {"suite": "all"}, errored=True
        ),
    ]

    copilot.handle_traces({}, spans, owner=None)

    trace = store.get_agent_trace(trace_id)
    assert len(trace) == 2
    assert trace[0]["tool"] == "read_file"
    assert trace[0]["args"] == {"path": "foo.py"}
    assert trace[0]["status"] == "success"
    assert trace[1]["tool"] == "run_tests"
    assert trace[1]["status"] == "error"


def test_malformed_span_does_not_crash_batch(isolated_sqlite_db):
    """A garbage/malformed span (missing traceId, non-dict, or bad JSON
    in a content attribute) must be skipped without raising, and must
    not prevent other valid spans in the same batch from being
    processed."""
    store = isolated_sqlite_db
    trace_id = "trace-4"
    good_chat = _chat_span(
        trace_id,
        "span-chat",
        "span-agent",
        input_messages=[{"role": "user", "content": "ok"}],
        output_messages=[{"role": "assistant", "content": "fine"}],
    )
    broken_chat = {
        "traceId": trace_id,
        "spanId": "span-chat-broken",
        "name": "chat",
        "startTimeUnixNano": "not-a-number",
        "endTimeUnixNano": "also-not-a-number",
        "attributes": [_attr("gen_ai.input.messages", "{not valid json")],
    }
    spans = [
        _invoke_agent_span(trace_id, "span-agent"),
        "this is not even a dict",
        {"name": "chat"},  # missing traceId entirely
        broken_chat,
        good_chat,
    ]

    # Must not raise.
    copilot.handle_traces({}, spans, owner=None)

    metrics = store.get_session_metrics(trace_id)
    assert metrics is not None
    breakdown = store.get_token_breakdown(trace_id)
    # Both the broken chat span (bad timestamps, unparseable messages)
    # and the good one get a turn appended (append_turn tolerates the
    # broken timestamps by falling back to latency_ms=0); the important
    # assertion is that processing continued past the broken span.
    assert len(breakdown) == 2
    assert breakdown[1]["input_tokens"] == 100
