"""GitHub Copilot OTLP mapper.

Maps GitHub Copilot's native OTLP export onto this repo's session/turn/
tool_call/context_block schema (metrics/store.py). See
docs/internal/OTLP_INTEGRATION_PLAN.md, "### GitHub Copilot" section, for
the underlying research (code.visualstudio.com/docs/agents/*,
docs.github.com/copilot/*) — NOT verified against a real captured
payload.

Structural summary:
  - Enable via COPILOT_OTEL_ENABLED=true + COPILOT_OTEL_CAPTURE_CONTENT=true
    (opt-in structured content capture) -> exposes gen_ai.input.messages,
    gen_ai.output.messages, gen_ai.tool.definitions,
    gen_ai.tool.call.arguments/.result as span attributes. Semantic
    content, already pre-structured (message arrays with a role/content
    shape broadly similar to the Anthropic Messages API), per OTel's
    gen_ai semantic conventions.
  - Traces are the primary structure: a hierarchical span tree per user
    turn -- invoke_agent (one per turn, our session/turn boundary
    signal) -> chat (one per LLM call, carries gen_ai.input.messages/
    gen_ai.output.messages/token usage) -> execute_tool (one per tool
    call, INCLUDING MCP-sourced calls, carries tool/server name, args,
    result, status, duration natively) -> execute_hook (skipped for v1,
    not required per the plan).
  - handle_traces is therefore the primary/authoritative entry point.
    handle_logs and handle_metrics are secondary/defensive paths only
    (see their docstrings for the specific judgment call on each).

UNVERIFIED ASSUMPTIONS (flag for follow-up once a real captured payload
exists):
  1. Exact attribute key names for token usage on a `chat` span are
     assumed to follow OTel gen_ai semconv naming
     (gen_ai.usage.input_tokens/output_tokens), with
     prompt_tokens/completion_tokens tried as a fallback pair since
     other gen_ai-adjacent exporters use that naming too.
  2. Session identity: prefers an explicit `session.id` /
     `gen_ai.conversation.id` / `conversation.id` attribute (checked on
     both the span and the resource) over the invoke_agent span's raw
     traceId, since one VS Code session likely spans multiple
     invoke_agent traces. Whether Copilot's real exporter stamps any
     such attribute at all is unverified -- if none is present, this
     mapper falls back to traceId per trace, meaning each invoke_agent
     trace becomes its own "session" until proven otherwise.
  3. gen_ai.input.messages/output.messages content-part shape (role,
     type: text/tool_call/tool_call_response/reasoning) is inferred from
     the general OTel gen_ai semantic conventions, not from a
     Copilot-specific captured example. Parsing is defensive (try/except
     per part) precisely because of this.
  4. `chat` span token usage is treated as authoritative and
     gen_ai.client.token.usage metrics are ignored entirely (see
     handle_metrics docstring) to avoid double-counting.
  5. Tool-call error/status detection uses the span's own OTLP `status`
     object (code 2 == ERROR) and an `exception`/`error`-named event as
     a fallback signal -- Copilot's docs don't spell out the exact
     status encoding used on execute_tool spans specifically.
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
    CATEGORY_USER,
    attrs_list_to_dict,
    estimate_tokens,
    truncate_content,
)

_SESSION_ID_ATTR_CANDIDATES = ("session.id", "gen_ai.conversation.id", "conversation.id")
_INPUT_TOKEN_ATTR_CANDIDATES = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens")
_OUTPUT_TOKEN_ATTR_CANDIDATES = ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens")
_MODEL_ATTR_CANDIDATES = ("gen_ai.request.model", "gen_ai.response.model")
_TOOL_CALL_TYPES = {"tool_call", "tool_use", "tool-call"}
_TOOL_RESULT_TYPES = {"tool_call_response", "tool_result", "tool-result"}
_REASONING_TYPES = {"reasoning", "thinking"}


def handle_logs(resource_attrs, log_records, owner):
    """No-op for v1 -- intentional, not an oversight.

    handle_traces (the invoke_agent/chat/execute_tool span tree) is the
    documented authoritative correlation mechanism for Copilot. Some
    OTel exporters additionally mirror gen_ai.* content onto log
    records, but whether Copilot's real exporter does this, and whether
    that content would be *additive* (e.g. something not present on any
    span) or a straight duplicate of what handle_traces already stores
    for the same trace, is unverified -- no captured payload exists to
    settle it either way. Rather than guess and risk double-appending
    context blocks for the same turn, this stays a deliberate no-op
    until a real payload answers the question. Revisit then.
    """
    return


def handle_metrics(resource_attrs, metrics, owner):
    """No-op for v1 -- intentional, not an oversight.

    gen_ai.client.token.usage / gen_ai.client.operation.duration
    datapoints would double-count against the per-`chat`-span usage
    attributes handle_traces already reads (see _handle_chat_span) if
    both paths fed append_turn for the same turn -- the chat span's own
    usage attributes are treated as primary, metrics as a secondary
    cross-check at best. Using metrics to backfill `model` for a session
    not yet seen via traces was considered but skipped: OTel's gen_ai
    metrics semconv gives datapoints no session/conversation-scoped
    correlation attribute, only per-request/model dimensions, so there's
    no safe way to attach a backfilled model to the right session_id
    without risking attaching it to the wrong one. Revisit if a captured
    payload shows a usable correlation attribute on these datapoints.
    """
    return


def handle_traces(resource_attrs, spans, owner):
    """Primary entry point. spans: raw OTLP JSON span objects, all
    already flattened out of their resourceSpans/scopeSpans wrapper by
    mcp_server/otlp/__init__.py. May contain only a slice of a full
    invoke_agent -> chat -> execute_tool tree (spans can arrive across
    separate HTTP posts) -- every span is handled independently off its
    own traceId, never assuming siblings are present in the same batch.
    """
    if not spans:
        return

    valid_spans = [s for s in spans if isinstance(s, dict) and s.get("traceId")]

    # Peek at any `chat` span's model attribute per traceId first, so
    # that session creation -- which may happen off an invoke_agent span
    # processed earlier in this same loop -- can still capture a model.
    # start_or_get_session only sets `model` on the first insert for a
    # given session_id; there's no update path, so this ordering matters.
    trace_model = {}
    for span in valid_spans:
        try:
            if span.get("name") != "chat":
                continue
            trace_id = span["traceId"]
            if trace_id in trace_model:
                continue
            attrs = attrs_list_to_dict(span.get("attributes", []))
            model = _first_present(attrs, _MODEL_ATTR_CANDIDATES)
            if model:
                trace_model[trace_id] = model
        except (KeyError, TypeError):
            continue

    trace_session = {}

    def _ensure_session(span_attrs, trace_id):
        if trace_id in trace_session:
            return trace_session[trace_id]
        session_id = _resolve_session_id(span_attrs, resource_attrs, trace_id)
        store.start_or_get_session(
            session_id, owner=owner, source="copilot", model=trace_model.get(trace_id)
        )
        trace_session[trace_id] = session_id
        return session_id

    for span in valid_spans:
        try:
            name = span.get("name")
            trace_id = span["traceId"]
            span_attrs = attrs_list_to_dict(span.get("attributes", []))

            if name == "invoke_agent":
                _ensure_session(span_attrs, trace_id)
            elif name == "chat":
                session_id = _ensure_session(span_attrs, trace_id)
                _handle_chat_span(span, span_attrs, session_id, owner)
            elif name == "execute_tool":
                session_id = _ensure_session(span_attrs, trace_id)
                _handle_tool_span(span, span_attrs, session_id, owner)
            # execute_hook: skipped for v1 -- not required, OTel path
            # already covers everything needed.
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, SessionOwnershipError):
            # One malformed span, or a span whose resolved session_id
            # collides with a different owner's existing session, must
            # never sink the rest of the batch.
            continue


def _resolve_session_id(span_attrs, resource_attrs, trace_id):
    for key in _SESSION_ID_ATTR_CANDIDATES:
        value = span_attrs.get(key) or resource_attrs.get(key)
        if value:
            return str(value)
    return trace_id


def _first_present(attrs, keys):
    for key in keys:
        value = attrs.get(key)
        if value is not None:
            return value
    return None


def _coerce_int(value):
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _handle_chat_span(span, span_attrs, session_id, owner):
    try:
        start_ns = int(span.get("startTimeUnixNano"))
        end_ns = int(span.get("endTimeUnixNano"))
        latency_ms = max(0, (end_ns - start_ns) / 1_000_000)
    except (TypeError, ValueError):
        latency_ms = 0

    input_tokens = _coerce_int(_first_present(span_attrs, _INPUT_TOKEN_ATTR_CANDIDATES))
    output_tokens = _coerce_int(_first_present(span_attrs, _OUTPUT_TOKEN_ATTR_CANDIDATES))

    # append_turn doesn't return the turn_n it assigned, so derive the
    # one it's about to assign (turns are sequential starting at 0) to
    # tag this span's context blocks with the right turn_n.
    turn_n = len(store.get_token_breakdown(session_id, owner=owner))

    store.append_turn(
        session_id,
        {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
        },
        owner=owner,
    )

    event_attr_dicts = [
        attrs_list_to_dict(ev.get("attributes", []))
        for ev in (span.get("events") or [])
        if isinstance(ev, dict)
    ]

    input_messages = _find_attr_value(span_attrs, event_attr_dicts, "gen_ai.input.messages")
    output_messages = _find_attr_value(span_attrs, event_attr_dicts, "gen_ai.output.messages")

    blocks = _messages_to_blocks(input_messages, turn_n) + _messages_to_blocks(
        output_messages, turn_n
    )
    if not blocks:
        return

    # Defensive fix for the same stateless-history risk flagged for the
    # Claude Code mapper: gen_ai.input.messages may represent the full
    # cumulative conversation rather than just this turn's new messages
    # (unverified either way for Copilot specifically). Only append the
    # tail beyond what's already stored for this session, so a later
    # chat span that repeats prior history doesn't duplicate blocks.
    already_stored = len(store.get_context_timeline(session_id, owner=owner))
    for block in blocks[already_stored:]:
        store.append_context_block(session_id, block, owner=owner)


def _find_attr_value(span_attrs, event_attr_dicts, key):
    if span_attrs.get(key):
        return span_attrs[key]
    for ev_attrs in event_attr_dicts:
        if ev_attrs.get(key):
            return ev_attrs[key]
    return None


def _messages_to_blocks(messages, turn_n):
    if messages is None:
        return []
    if isinstance(messages, str):
        try:
            messages = json.loads(messages)
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(messages, list):
        return []

    blocks = []
    for msg in messages:
        try:
            blocks.extend(_message_to_blocks(msg, turn_n))
        except (KeyError, TypeError):
            continue
    return blocks


def _message_to_blocks(msg, turn_n):
    if not isinstance(msg, dict):
        return []
    role = str(msg.get("role", "")).lower()
    content = msg.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            pass
    parts = content if isinstance(content, list) else [content]

    blocks = []
    for part in parts:
        text, category = _part_to_text_and_category(part, role)
        if not text:
            continue
        blocks.append(
            {
                "category": category,
                "label": f"copilot.{category}",
                "char_count": len(text),
                "token_estimate": estimate_tokens(text),
                "turn_n": turn_n,
                "status": None,
                "content": truncate_content(text),
            }
        )
    return blocks


def _part_to_text_and_category(part, role):
    if isinstance(part, str):
        return part, _category_for_role(role)
    if not isinstance(part, dict):
        return "", _category_for_role(role)

    part_type = str(part.get("type", "")).lower()
    text = part.get("text")
    if text is None:
        text = part.get("content")
    if text is None:
        text = part.get("arguments")
    if text is None:
        text = part.get("result")
    if text is None:
        text = ""
    if not isinstance(text, str):
        try:
            text = json.dumps(text)
        except TypeError:
            text = str(text)

    if part_type in _TOOL_CALL_TYPES:
        return text, CATEGORY_TOOL_CALL
    if part_type in _TOOL_RESULT_TYPES:
        return text, CATEGORY_TOOL_RESULT
    if part_type in _REASONING_TYPES:
        return text, CATEGORY_REASONING
    return text, _category_for_role(role)


def _category_for_role(role):
    if role == "user":
        return CATEGORY_USER
    if role == "system":
        return CATEGORY_SYSTEM
    if role == "tool":
        return CATEGORY_TOOL_RESULT
    return CATEGORY_ANSWER


def _handle_tool_span(span, span_attrs, session_id, owner):
    tool_name = (
        span_attrs.get("gen_ai.tool.name")
        or span_attrs.get("tool.name")
        or span.get("name")
        or "unknown_tool"
    )
    args = _parse_maybe_json(span_attrs.get("gen_ai.tool.call.arguments"))
    status = "error" if _tool_span_errored(span) else "success"
    store.append_tool_call(
        session_id,
        {"tool": tool_name, "args": args if args is not None else {}, "status": status},
        owner=owner,
    )


def _parse_maybe_json(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value
    return value


def _tool_span_errored(span):
    status = span.get("status")
    if isinstance(status, dict):
        code = status.get("code")
        if code in (2, "STATUS_CODE_ERROR", "ERROR", "Error"):
            return True
    for ev in span.get("events") or []:
        if isinstance(ev, dict) and str(ev.get("name", "")).lower() in ("exception", "error"):
            return True
    return False
