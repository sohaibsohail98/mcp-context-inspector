"""Claude Code OTLP mapper.

Parses Claude Code's native OpenTelemetry export (`http/json` protocol,
`OTEL_LOG_RAW_API_BODIES=1` inline mode — see docs/internal/OTLP_INTEGRATION_PLAN.md,
"### Claude Code" section) into the append_*/start_or_get_session calls in
metrics/store.py.

Bodies are the literal Anthropic Messages API JSON. The Messages API is
stateless: every api_request_body event's `messages[]` is the FULL
cumulative conversation history up to that point, not just new content.
To avoid duplicating every prior turn's context_blocks on every request,
we diff the freshly-walked block list against store.get_context_timeline()
(the already-persisted blocks for this session) and only append the tail
beyond what's already stored. This also makes reprocessing a retried/
duplicate batch safe.

api_response_body content, by contrast, is genuinely new every time (the
assistant's fresh reply) and is always appended directly, no diffing.

Verified against a real captured payload (CLAUDE_CODE_ENABLE_TELEMETRY=1,
OTEL_LOG_RAW_API_BODIES=1, claude-code 2.1.233), 2026-08-20:
  - session.id, model, and event.name are all stamped on the log
    record's own attributes (not just resource attributes) — confirmed.
  - event.name values are the bare event name (`"api_request_body"`,
    not `"claude_code.api_request_body"`) — confirmed, matches
    _handle_log_record's dispatch.
  - the raw Messages API JSON is in the log record's `body` ATTRIBUTE
    (`attrs["body"]`), not the LogRecord's own top-level `body` field —
    this was WRONG in the original assumption and silently broke every
    request/response body handler; fixed in _parse_body.

Still unverified (no tool call happened in the captured session):
  - exact attribute key names on tool_result events (tool_name/success/
    duration_ms/error_type below are the plausible OTel-semantic-
    convention-style names, not confirmed)
  - exact latency/duration attribute name on api_response_body records
    (not present as a top-level attribute in the captured payload;
    latency/duration_ms/timestamps live inside the separate
    claude_code.api_request event instead — see _handle_response_body)
"""

import json

from metrics import store
from metrics.errors import SessionOwnershipError
from mcp_server.otlp.common import (
    CATEGORY_ANSWER,
    CATEGORY_REASONING,
    CATEGORY_SYSTEM,
    CATEGORY_TOOL_CALL,
    CATEGORY_TOOL_RESULT,
    CATEGORY_TOOLS,
    CATEGORY_USER,
    attrs_list_to_dict,
    estimate_tokens,
)

# Narrow, deliberate exception set for per-record try/except in the batch
# loops below: KeyError/TypeError cover malformed/missing dict shape,
# json.JSONDecodeError covers a body string that isn't valid JSON (e.g. a
# truncated inline body, or a body_ref-only record with no real body — see
# _parse_body). SessionOwnershipError covers a payload whose session.id
# collides with a different owner's existing session — skip that record
# rather than writing into someone else's session or crashing the batch.
# One malformed/unauthorized record must never sink the rest of the batch.
_SKIP_EXCEPTIONS = (KeyError, TypeError, json.JSONDecodeError, SessionOwnershipError)


# ---------------------------------------------------------------------------
# Block construction helpers
# ---------------------------------------------------------------------------


def _mk_block(category, label, text, turn_n, status=None):
    text = text or ""
    return {
        "category": category,
        "label": label,
        "char_count": len(text),
        "token_estimate": estimate_tokens(text),
        "turn_n": turn_n,
        "status": status,
    }


def _mk_text_block(role, text, turn_n):
    if role == "user":
        return _mk_block(CATEGORY_USER, "User message", text, turn_n)
    return _mk_block(CATEGORY_ANSWER, "Assistant response", text, turn_n)


def _mk_tool_use_block(item, turn_n):
    name = item.get("name", "unknown_tool")
    args_text = json.dumps(item.get("input", {}))
    return _mk_block(CATEGORY_TOOL_CALL, f"Tool call: {name}", args_text, turn_n)


def _join_text_blocks(items):
    """system/tool_result content can be a plain string OR a list of
    content blocks (e.g. `[{"type": "text", "text": "..."}]`, used when
    cache_control markers are attached) — flatten either shape to text."""
    texts = []
    for item in items or []:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text", ""))
        elif isinstance(item, str):
            texts.append(item)
    return "\n".join(t for t in texts if t)


def _mk_tool_result_block(item, turn_n):
    content = item.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = _join_text_blocks(content)
    else:
        text = ""
    tool_use_id = item.get("tool_use_id", "")
    label = f"Tool result ({tool_use_id})" if tool_use_id else "Tool result"
    status = "error" if item.get("is_error") else None
    return _mk_block(CATEGORY_TOOL_RESULT, label, text, turn_n, status=status)


def _mk_reasoning_block(item, turn_n):
    """Extended-thinking content is unconditionally redacted by Claude
    Code even with raw bodies on (confirmed in the plan) — there is no
    real thinking text to show. Use whatever placeholder/redaction-marker
    text is present for sizing; if there's truly none, force a minimal
    nonzero token_estimate (never 0 — a real reasoning block did consume
    real tokens even though we can't see the content)."""
    text = item.get("thinking") or item.get("data") or ""
    if not isinstance(text, str):
        text = str(text)
    block = _mk_block(CATEGORY_REASONING, "reasoning (redacted)", text, turn_n)
    if block["token_estimate"] == 0:
        block["token_estimate"] = 1
    return block


def _blocks_from_message(msg, turn_n):
    role = msg.get("role")
    content = msg.get("content")
    blocks = []
    if isinstance(content, str):
        blocks.append(_mk_text_block(role, content, turn_n))
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                blocks.append(_mk_text_block(role, item.get("text", ""), turn_n))
            elif item_type == "tool_use":
                blocks.append(_mk_tool_use_block(item, turn_n))
            elif item_type == "tool_result":
                blocks.append(_mk_tool_result_block(item, turn_n))
            elif item_type in ("thinking", "redacted_thinking"):
                blocks.append(_mk_reasoning_block(item, turn_n))
            # Unrecognized content block types are skipped rather than
            # guessed at.
    return blocks


def _is_genuine_user_turn(msg):
    """The Anthropic Messages API sends tool_result content back inside
    a message with role='user' (this is documented, stable API
    behavior) — a naive 'every role==user message starts a new turn'
    rule double-counts that as a second, spurious turn for every single
    tool call, since the real new user turn only happens once per
    actual round-trip. A message counts as starting a genuine new turn
    only if it carries real user-authored content (a plain string, or
    at least one non-tool_result content block) — a message that's
    purely tool_result blocks stays attached to the turn already in
    progress."""
    content = msg.get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        return any(
            isinstance(item, dict) and item.get("type") != "tool_result" for item in content
        )
    return False


def _walk_request_body(body):
    """Builds the FULL ordered block list implied by one api_request_body
    payload: system -> tools -> each messages[] entry's content, in
    order. turn_n increments each time a new genuine user turn is
    crossed (see _is_genuine_user_turn) — assistant content and
    tool_result-only messages share that same turn_n until the next
    real user message. Caller is responsible for diffing this against
    what's already stored (see module docstring)."""
    blocks = []

    system = body.get("system")
    if system:
        text = system if isinstance(system, str) else _join_text_blocks(system)
        if text:
            blocks.append(_mk_block(CATEGORY_SYSTEM, "System prompt", text, None))

    tools = body.get("tools")
    if tools:
        text = json.dumps(tools)
        blocks.append(_mk_block(CATEGORY_TOOLS, "Tool specifications", text, None))

    turn_n = -1
    for msg in body.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user" and _is_genuine_user_turn(msg):
            turn_n += 1
        blocks.extend(_blocks_from_message(msg, max(turn_n, 0)))

    return blocks


def _parse_body(attrs):
    """Returns the parsed JSON dict body, or None if unavailable.

    CONFIRMED against a real captured payload (CLAUDE_CODE_ENABLE_TELEMETRY=1,
    OTEL_LOG_RAW_API_BODIES=1): the raw Messages API JSON lives in the log
    record's `body` ATTRIBUTE (inside `attributes`, alongside `body_length`/
    `model`/etc — i.e. `attrs["body"]`, already unwrapped by
    attrs_list_to_dict), not in the LogRecord's own top-level `body` field.
    That field instead carries the dotted event name
    (`"claude_code.api_request_body"`) as its stringValue — reading it as
    the payload silently no-ops every request/response body (the
    json.JSONDecodeError falls into _SKIP_EXCEPTIONS).

    `None` covers both a genuinely missing body and the file-mode
    `body_ref` case (this repo's onboarding always uses inline mode, but a
    record with only body_ref and no real body should degrade to
    content-unavailable, not crash)."""
    body = attrs.get("body")
    if isinstance(body, str):
        return json.loads(body)
    if isinstance(body, dict):
        return body
    return None


# ---------------------------------------------------------------------------
# Per-event handling
# ---------------------------------------------------------------------------


def _current_turn_n(session_id, owner):
    """Best-effort 'current turn' for blocks appended from a response
    body, which arrives without its own messages[] array to derive
    turn_n from positionally. Uses the highest turn_n already recorded
    for this session (falls back to 0 for a session with no blocks yet)."""
    timeline = store.get_context_timeline(session_id, owner=owner)
    turn_ns = [b["turn_n"] for b in timeline if b.get("turn_n") is not None]
    return max(turn_ns) if turn_ns else 0


def _handle_request_body(session_id, attrs, owner):
    body = _parse_body(attrs)
    if not isinstance(body, dict):
        return  # content unavailable (e.g. body_ref-only) — nothing to walk
    fresh_blocks = _walk_request_body(body)
    existing = store.get_context_timeline(session_id, owner=owner)
    tail = fresh_blocks[len(existing):]
    for block in tail:
        store.append_context_block(session_id, block, owner=owner)


def _handle_response_body(session_id, attrs, owner):
    body = _parse_body(attrs)
    if not isinstance(body, dict):
        return  # content unavailable

    turn_n = _current_turn_n(session_id, owner)
    content = body.get("content")
    if isinstance(content, list):
        for block in _blocks_from_message({"role": "assistant", "content": content}, turn_n):
            store.append_context_block(session_id, block, owner=owner)

    usage = body.get("usage")
    if isinstance(usage, dict):
        # Exact-count path per the plan: prefer the response's own usage
        # block over any char-based estimate. Latency: no confirmed
        # attribute name for this on api_response_body records (see
        # module docstring) — try a couple of plausible ones, else 0
        # rather than fabricating a number.
        latency_ms = attrs.get("duration_ms") or attrs.get("latency_ms") or 0
        store.append_turn(
            session_id,
            {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "latency_ms": latency_ms,
                "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_input_tokens": usage.get("cache_creation_input_tokens", 0),
            },
            owner=owner,
        )


def _handle_tool_result(session_id, attrs, owner):
    # Attribute key names here are unverified against a real payload (see
    # module docstring) — tool_name/success/duration_ms/error_type follow
    # plausible OTel semantic-convention naming, not a confirmed schema.
    tool_name = attrs.get("tool_name") or attrs.get("tool.name") or "unknown_tool"
    status = "success" if attrs.get("success") else "error"
    latency_ms = attrs.get("duration_ms") or attrs.get("latency_ms") or 0
    store.append_tool_call(
        session_id,
        {"tool": tool_name, "args": {}, "status": status, "latency_ms": latency_ms},
        owner=owner,
    )


def _handle_log_record(resource_attrs, record, owner):
    attrs = attrs_list_to_dict(record.get("attributes", []))
    # Correlation identifiers are unverified as to which level Claude
    # Code stamps them at (log-record vs. resource) — check both, record
    # attrs first.
    session_id = attrs.get("session.id") or resource_attrs.get("session.id")
    if not session_id:
        return

    model = attrs.get("model") or resource_attrs.get("model")
    store.start_or_get_session(session_id, owner=owner, source="claude_code", model=model)

    event_name = attrs.get("event.name")
    if event_name == "api_request_body":
        _handle_request_body(session_id, attrs, owner)
    elif event_name == "api_response_body":
        _handle_response_body(session_id, attrs, owner)
    elif event_name == "tool_result":
        _handle_tool_result(session_id, attrs, owner)
    # user_prompt / assistant_response / mcp_server_connection: not
    # required for v1 correctness (request/response bodies already carry
    # this content); mcp_server_connection health surfacing is deferred
    # per the plan's "Maximizing telemetry" section. Skipped here on
    # purpose, not an oversight.


def handle_logs(resource_attrs, log_records, owner):
    for record in log_records:
        try:
            _handle_log_record(resource_attrs, record, owner)
        except _SKIP_EXCEPTIONS:
            continue


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _handle_metric(resource_attrs, metric, owner):
    if metric.get("name") != "claude_code.token.usage":
        # claude_code.cost.usage, .lines_of_code.count, .commit.count, etc.
        # are real and confirmed per the plan's "Maximizing telemetry"
        # section but deferred past this v1 pass — not wired to a store
        # call yet.
        return
    datapoints = (metric.get("sum") or metric.get("gauge") or {}).get("dataPoints", [])
    for dp in datapoints:
        dp_attrs = attrs_list_to_dict(dp.get("attributes", []))
        session_id = dp_attrs.get("session.id") or resource_attrs.get("session.id")
        if not session_id:
            continue
        model = dp_attrs.get("model") or resource_attrs.get("model")
        # Judgment call (documented per the task instructions): metrics
        # are treated as a supplementary/cross-check signal only, not fed
        # into append_turn. The body-walking path in handle_logs already
        # gets exact token counts from each api_response_body's usage
        # block, which carries the same numbers this metric does
        # (input/output/cacheRead/cacheCreation) plus real content.
        # Calling append_turn from here too would double-count every
        # turn's tokens. handle_metrics's only job for v1 is to make sure
        # a session/model exists (backfilling model if a session hasn't
        # been seen via logs yet, e.g. metrics batch arrives first).
        store.start_or_get_session(session_id, owner=owner, source="claude_code", model=model)


def handle_metrics(resource_attrs, metrics, owner):
    for metric in metrics:
        try:
            _handle_metric(resource_attrs, metric, owner)
        except _SKIP_EXCEPTIONS:
            continue
