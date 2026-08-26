"""Tests for mcp_server/otlp/claude_code.py.

Fixtures match the real wire shape confirmed via a live local capture
(CLAUDE_CODE_ENABLE_TELEMETRY=1, OTEL_LOG_RAW_API_BODIES=1, claude-code
2.1.233, 2026-08-20) — see claude_code.py's module docstring "Verified"
section. Notably: the raw body lives in the log record's `body`
ATTRIBUTE (`attrs["body"]`), not the LogRecord's own top-level `body`
field (which instead carries the dotted event name as its stringValue,
e.g. `"claude_code.api_request_body"` — reproduced here too, since a
fixture that only sets the attribute wouldn't catch a regression back to
reading the wrong field).
"""

import json

from mcp_server.otlp import claude_code


def _log_record(event_name, body, session_id="sess-1", **extra_attrs):
    body_str = json.dumps(body) if not isinstance(body, str) else body
    attrs = [
        {"key": "event.name", "value": {"stringValue": event_name}},
        {"key": "session.id", "value": {"stringValue": session_id}},
        {"key": "body", "value": {"stringValue": body_str}},
    ]
    for k, v in extra_attrs.items():
        if isinstance(v, bool):
            attrs.append({"key": k, "value": {"boolValue": v}})
        elif isinstance(v, int):
            attrs.append({"key": k, "value": {"intValue": str(v)}})
        else:
            attrs.append({"key": k, "value": {"stringValue": str(v)}})
    return {
        "timeUnixNano": "1000000000",
        "attributes": attrs,
        # Real Claude Code stamps the dotted event name here, NOT the
        # payload — confirmed via live capture. Kept realistic so a
        # regression back to reading this field would fail these tests.
        "body": {"stringValue": f"claude_code.{event_name}"},
    }


RESOURCE_ATTRS = {"service.name": "claude-code"}


def test_request_response_pair_produces_blocks_and_turn(isolated_sqlite_db):
    """A single request+response pair should produce system/tools/user
    blocks from the request and an answer block + a turn from the
    response, with exact token counts taken from the response's usage
    block."""
    store = isolated_sqlite_db

    request_body = {
        "model": "claude-sonnet-4-6",
        "system": "You are a helpful assistant.",
        "tools": [{"name": "list_services", "description": "List services"}],
        "messages": [
            {"role": "user", "content": "What services are degraded?"},
        ],
    }
    response_body = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Service X is degraded."}],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 20,
            "cache_read_input_tokens": 10,
            "cache_creation_input_tokens": 0,
        },
    }

    records = [
        _log_record("api_request_body", request_body),
        _log_record("api_response_body", response_body, duration_ms=800),
    ]
    claude_code.handle_logs(RESOURCE_ATTRS, records, owner=None)

    timeline = store.get_context_timeline("sess-1")
    categories = [b["category"] for b in timeline]
    assert categories == ["system", "tools", "user", "answer"]
    assert timeline[0]["label"] == "System prompt"
    assert timeline[1]["label"] == "Tool specifications"
    assert timeline[2]["turn_n"] == 0
    assert timeline[3]["turn_n"] == 0

    breakdown = store.get_token_breakdown("sess-1")
    assert len(breakdown) == 1
    assert breakdown[0]["input_tokens"] == 150
    assert breakdown[0]["output_tokens"] == 20
    assert breakdown[0]["cache_read_input_tokens"] == 10
    assert breakdown[0]["latency_ms"] == 800


def test_second_request_with_resent_history_does_not_duplicate(isolated_sqlite_db):
    """The Messages API is stateless: the second request's messages[]
    resends the entire prior history plus new content. The mapper must
    diff against what's already stored and only append the new tail."""
    store = isolated_sqlite_db

    first_request = {
        "system": "sys prompt",
        "messages": [{"role": "user", "content": "first question"}],
    }
    first_response = {
        "content": [{"type": "text", "text": "first answer"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    second_request = {
        "system": "sys prompt",
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
            {"role": "user", "content": "second question"},
        ],
    }
    second_response = {
        "content": [{"type": "text", "text": "second answer"}],
        "usage": {"input_tokens": 20, "output_tokens": 8},
    }

    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [
            _log_record("api_request_body", first_request),
            _log_record("api_response_body", first_response),
        ],
        owner=None,
    )
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [
            _log_record("api_request_body", second_request),
            _log_record("api_response_body", second_response),
        ],
        owner=None,
    )

    timeline = store.get_context_timeline("sess-1")
    # sys prompt, first user, first answer (from response), second user
    # (new tail from 2nd request), second answer (from 2nd response).
    # The 2nd request's own resent "first answer" content must NOT
    # duplicate the block already appended from the first response.
    labels = [b["label"] for b in timeline]
    assert labels.count("Assistant response") == 2
    assert labels.count("User message") == 2
    assert labels.count("System prompt") == 1

    breakdown = store.get_token_breakdown("sess-1")
    assert len(breakdown) == 2
    assert breakdown[1]["input_tokens"] == 20


def test_tool_result_event_appends_tool_call(isolated_sqlite_db):
    store = isolated_sqlite_db
    record = _log_record(
        "tool_result",
        body="",
        tool_name="list_services",
        success=True,
        duration_ms=120,
    )
    claude_code.handle_logs(RESOURCE_ATTRS, [record], owner=None)

    trace = store.get_agent_trace("sess-1")
    assert len(trace) == 1
    assert trace[0]["tool"] == "list_services"
    assert trace[0]["args"] == {}
    assert trace[0]["status"] == "success"
    assert trace[0]["latency_ms"] == 120


def test_redacted_thinking_block_gets_reasoning_category(isolated_sqlite_db):
    """Extended-thinking content is unconditionally redacted even with
    raw bodies on — must still categorize as CATEGORY_REASONING with
    status="redacted" (so the dashboard's redacted-block styling
    applies), never a zero token_estimate, and no stored content (the
    placeholder text isn't real content worth showing on expand)."""
    store = isolated_sqlite_db

    request_body = {
        "messages": [{"role": "user", "content": "solve this"}],
    }
    response_body = {
        "content": [
            {"type": "redacted_thinking", "data": ""},
            {"type": "text", "text": "here's the answer"},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [
            _log_record("api_request_body", request_body),
            _log_record("api_response_body", response_body),
        ],
        owner=None,
    )

    timeline = store.get_context_timeline("sess-1")
    reasoning_blocks = [b for b in timeline if b["category"] == "reasoning"]
    assert len(reasoning_blocks) == 1
    assert reasoning_blocks[0]["label"] == "reasoning"
    assert reasoning_blocks[0]["status"] == "redacted"
    assert reasoning_blocks[0]["token_estimate"] >= 1
    assert reasoning_blocks[0]["content"] is None


def test_malformed_record_does_not_crash_batch(isolated_sqlite_db):
    """One malformed log record (invalid JSON body) must be skipped, not
    crash the whole batch — other valid records in the same batch must
    still be processed."""
    store = isolated_sqlite_db

    good_request = {
        "messages": [{"role": "user", "content": "hello"}],
    }
    malformed = _log_record("api_request_body", body="not valid json {{{", session_id="sess-1")
    good = _log_record("api_request_body", good_request, session_id="sess-1")

    # handle_logs must not raise despite the malformed record.
    claude_code.handle_logs(RESOURCE_ATTRS, [malformed, good], owner=None)

    timeline = store.get_context_timeline("sess-1")
    assert any(b["category"] == "user" for b in timeline)


def test_tool_result_message_does_not_inflate_turn_number(isolated_sqlite_db):
    """A tool_result content block arrives inside a role='user' message
    per the real Anthropic Messages API — a naive 'every user-role
    message starts a new turn' rule would double-count that as a second
    turn per tool call. Regression test for that bug found in review:
    one real user turn (question -> tool call -> tool result -> answer)
    must produce exactly one turn_n value across all its blocks."""
    store = isolated_sqlite_db
    request_body = {
        "messages": [
            {"role": "user", "content": "What services are running?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "list_services", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "svc-a, svc-b"}]},
        ]
    }
    record = _log_record("api_request_body", request_body, session_id="sess-turns")
    claude_code.handle_logs(RESOURCE_ATTRS, [record], owner=None)

    timeline = store.get_context_timeline("sess-turns")
    turn_numbers = {b["turn_n"] for b in timeline}
    assert turn_numbers == {0}


def test_first_user_message_backfills_session_prompt(isolated_sqlite_db):
    """OTLP sessions are created via start_or_get_session with
    prompt=None (unlike record_session's one-shot Bedrock path, which
    gets its prompt as a direct argument) — nothing else ever populates
    it, so the dashboard showed "(no prompt)" for every OTLP session even
    though the real conversation content was captured correctly.
    append_context_block must backfill sessions.prompt from the first
    real user-message context block."""
    store = isolated_sqlite_db

    request_body = {
        "messages": [{"role": "user", "content": "What services are degraded right now?"}],
    }
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", request_body, session_id="sess-prompt")],
        owner=None,
    )

    metrics = store.get_session_metrics("sess-prompt")
    assert metrics["prompt_metrics"]["prompt"] == "What services are degraded right now?"


def test_later_user_message_does_not_overwrite_first_prompt(isolated_sqlite_db):
    """Turn 0's user message sets sessions.prompt; a second/later turn's
    user message must never clobber it — the prompt column is meant to
    reflect how the session started, not its most recent turn."""
    store = isolated_sqlite_db

    first_request = {
        "messages": [{"role": "user", "content": "first question"}],
    }
    first_response = {
        "content": [{"type": "text", "text": "first answer"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    second_request = {
        "messages": [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": [{"type": "text", "text": "first answer"}]},
            {"role": "user", "content": "second question, totally different"},
        ],
    }

    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [
            _log_record("api_request_body", first_request, session_id="sess-prompt-2"),
            _log_record("api_response_body", first_response, session_id="sess-prompt-2"),
        ],
        owner=None,
    )
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", second_request, session_id="sess-prompt-2")],
        owner=None,
    )

    metrics = store.get_session_metrics("sess-prompt-2")
    assert metrics["prompt_metrics"]["prompt"] == "first question"


def test_shape_shift_does_not_triple_user_message_blocks(isolated_sqlite_db):
    """Regression test for the reported bug: real Claude Code sessions
    inject content (e.g. a <system-reminder> block carrying environment
    context) AHEAD of already-seen text in a later request, changing the
    SHAPE of messages[] between requests in a way that isn't a strict
    tail-append. The old length-based slice (fresh_blocks[len(existing):])
    would then misalign and re-append already-stored blocks as
    duplicates. The occurrence-indexed identity dedup must handle this
    correctly.

    Request 1 sends a single <session>-wrapped user message. Request 2
    sends the SAME cumulative history reshaped: a new
    <system-reminder>-wrapped message is placed BEFORE the original
    <session>-wrapped message (chosen over "after" because that's the
    ordering that actually breaks a length-slice diff here — a suffix
    slice starting at len(existing)==1 would, with "after" placement,
    still land on genuinely new content; with "before" placement it
    instead lands on the tail of the ALREADY-SEEN <session> message,
    which is exactly the duplication bug being reproduced)."""
    store = isolated_sqlite_db

    first_request = {
        "messages": [
            {"role": "user", "content": "<session>\nWhat's the deploy process?\n</session>"},
        ],
    }
    second_request = {
        "messages": [
            {
                "role": "user",
                "content": (
                    "<system-reminder>\nSome injected context here.\n</system-reminder>\n\n"
                    "What's the deploy process?"
                ),
            },
            {"role": "user", "content": "<session>\nWhat's the deploy process?\n</session>"},
        ],
    }

    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", first_request, session_id="sess-shape")],
        owner=None,
    )
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", second_request, session_id="sess-shape")],
        owner=None,
    )

    timeline = store.get_context_timeline("sess-shape")
    user_blocks = [b for b in timeline if b["category"] == "user"]
    assert len(user_blocks) == 2

    seen = set()
    for b in user_blocks:
        key = (b["category"], b["label"], b["content"])
        assert key not in seen, f"duplicate block found: {key}"
        seen.add(key)


def test_third_identical_resend_appends_nothing(isolated_sqlite_db):
    """Pure idempotency: if Claude Code resends the exact same cumulative
    history a third time totally unchanged, no new blocks should be
    appended at all."""
    store = isolated_sqlite_db

    request_body = {
        "messages": [
            {"role": "user", "content": "<system-reminder>\nSome injected context here.\n</system-reminder>\n\nWhat's the deploy process?"},
            {"role": "user", "content": "<session>\nWhat's the deploy process?\n</session>"},
        ],
    }

    for _ in range(3):
        claude_code.handle_logs(
            RESOURCE_ATTRS,
            [_log_record("api_request_body", request_body, session_id="sess-idempotent")],
            owner=None,
        )

    timeline = store.get_context_timeline("sess-idempotent")
    user_blocks = [b for b in timeline if b["category"] == "user"]
    assert len(user_blocks) == 2


def test_byte_identical_tool_results_in_same_request_are_both_stored(isolated_sqlite_db):
    """Legitimate-repeat edge case: two tool_result blocks in the SAME
    request that happen to have byte-identical content (e.g. two tool
    calls both returning "OK") are genuinely distinct occurrences and
    must both be stored — the occurrence-index approach must not dedupe
    them away as if they were the same occurrence."""
    store = isolated_sqlite_db

    request_body = {
        "messages": [
            {"role": "user", "content": "run both checks"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "check_a", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "check_b", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "OK"},
                    {"type": "tool_result", "tool_use_id": "t2", "content": "OK"},
                ],
            },
        ],
    }
    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", request_body, session_id="sess-repeat")],
        owner=None,
    )

    timeline = store.get_context_timeline("sess-repeat")
    tool_results = [b for b in timeline if b["category"] == "tool_result"]
    # Distinct labels (tool_use_id is embedded in the label), so this
    # isn't even a same-identity case — but assert content explicitly too.
    assert len(tool_results) == 2
    assert all(b["content"] == "OK" for b in tool_results)


def test_same_identity_repeat_admits_second_genuine_occurrence(isolated_sqlite_db):
    """Exercises the occurrence-index Counter logic directly for blocks
    that DO share an identical (category, label, content) key — e.g. two
    plain assistant text replies that happen to be byte-identical
    ("Done."). Request 1 sends one such assistant turn; request 2 resends
    that turn PLUS a second, genuinely new occurrence of the exact same
    text later in the conversation. The dedup must admit exactly one new
    block (the 2nd occurrence), not zero (over-dedup) and not two
    (under-dedup of the already-stored 1st occurrence)."""
    store = isolated_sqlite_db

    first_request = {
        "messages": [
            {"role": "user", "content": "ping"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ],
    }
    second_request = {
        "messages": [
            {"role": "user", "content": "ping"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            {"role": "user", "content": "ping again"},
            {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
        ],
    }

    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", first_request, session_id="sess-same-identity")],
        owner=None,
    )
    timeline_after_first = store.get_context_timeline("sess-same-identity")
    assert len([b for b in timeline_after_first if b["label"] == "Assistant response"]) == 1

    claude_code.handle_logs(
        RESOURCE_ATTRS,
        [_log_record("api_request_body", second_request, session_id="sess-same-identity")],
        owner=None,
    )
    timeline_after_second = store.get_context_timeline("sess-same-identity")
    done_blocks = [b for b in timeline_after_second if b["label"] == "Assistant response"]
    assert len(done_blocks) == 2
    assert all(b["content"] == "Done." for b in done_blocks)


def test_body_is_read_from_attribute_not_log_record_body_field(isolated_sqlite_db):
    """Regression test for a bug found via a real local capture
    (CLAUDE_CODE_ENABLE_TELEMETRY=1, OTEL_LOG_RAW_API_BODIES=1): the raw
    Messages API JSON is in the log record's `body` ATTRIBUTE, not the
    LogRecord's own top-level `body` field — that field instead carries
    the dotted event name (`"claude_code.api_request_body"`) as its
    stringValue. Reading the wrong field silently no-ops every
    request/response body (caught by _SKIP_EXCEPTIONS), so no exception
    is raised and no test failure appears without an explicit check like
    this one. Hand-built here (not via the shared _log_record helper) so
    this test still catches a regression even if the helper changes."""
    store = isolated_sqlite_db
    request_body = {"messages": [{"role": "user", "content": "hello from a real capture shape"}]}
    record = {
        "timeUnixNano": "1000000000",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "api_request_body"}},
            {"key": "session.id", "value": {"stringValue": "sess-real-shape"}},
            {"key": "body", "value": {"stringValue": json.dumps(request_body)}},
        ],
        # The LogRecord's own body — a real capture puts the dotted
        # event name here, NOT the payload.
        "body": {"stringValue": "claude_code.api_request_body"},
    }

    claude_code.handle_logs(RESOURCE_ATTRS, [record], owner=None)

    timeline = store.get_context_timeline("sess-real-shape")
    assert any(b["category"] == "user" for b in timeline)


def test_connection_only_event_does_not_create_an_empty_session(isolated_sqlite_db):
    """Regression test for a real bug found via live capture (2026-08-27,
    claude-code 2.1.246): running /clear starts a brand-new session_id
    client-side, and if no real prompt follows, that session's only log
    record is a lone mcp_server_connection event, a connection-health
    ping, not a turn. Before the fix, _handle_log_record created a
    session row (start_or_get_session) for ANY event with a session_id,
    so this left a permanent, empty (prompt=None, model=None,
    status="open" forever) row that nothing ever backfilled, since no
    other event for that session_id was ever going to arrive. Reported
    by a user as "sessions missing/showing up broken" in their history."""
    store = isolated_sqlite_db

    record = {
        "timeUnixNano": "1000000000",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "mcp_server_connection"}},
            {"key": "session.id", "value": {"stringValue": "sess-clear-only"}},
        ],
        "body": {"stringValue": "claude_code.mcp_server_connection"},
    }

    claude_code.handle_logs(RESOURCE_ATTRS, [record], owner=None)

    assert store.get_session_metrics("sess-clear-only") is None


def test_real_turn_after_connection_only_event_still_creates_session(isolated_sqlite_db):
    """The fix only skips session creation for non-turn events — a
    session_id that sends mcp_server_connection first and then a real
    turn must still land normally."""
    store = isolated_sqlite_db

    connection_record = {
        "timeUnixNano": "1000000000",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "mcp_server_connection"}},
            {"key": "session.id", "value": {"stringValue": "sess-then-real"}},
        ],
        "body": {"stringValue": "claude_code.mcp_server_connection"},
    }
    request_body = {"messages": [{"role": "user", "content": "a real prompt after all"}]}
    request_record = _log_record("api_request_body", request_body, session_id="sess-then-real")

    claude_code.handle_logs(RESOURCE_ATTRS, [connection_record, request_record], owner=None)

    assert store.get_session_metrics("sess-then-real") is not None


def _token_usage_metric(session_id, value, value_key="asInt"):
    return {
        "name": "claude_code.token.usage",
        "sum": {
            "dataPoints": [
                {
                    "attributes": [{"key": "session.id", "value": {"stringValue": session_id}}],
                    value_key: value,
                }
            ]
        },
    }


def test_zero_value_metric_does_not_create_a_session(isolated_sqlite_db):
    """A claude_code.token.usage datapoint with a zero value carries no
    real usage to report — same reasoning as the logs-side fix above,
    applied to the metrics path."""
    store = isolated_sqlite_db

    claude_code.handle_metrics(RESOURCE_ATTRS, [_token_usage_metric("sess-zero-metric", 0)], owner=None)

    assert store.get_session_metrics("sess-zero-metric") is None


def test_nonzero_value_metric_still_creates_a_session(isolated_sqlite_db):
    store = isolated_sqlite_db

    claude_code.handle_metrics(RESOURCE_ATTRS, [_token_usage_metric("sess-real-metric", 42)], owner=None)

    assert store.get_session_metrics("sess-real-metric") is not None
