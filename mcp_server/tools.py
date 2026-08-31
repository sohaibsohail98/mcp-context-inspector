"""The 8 @server.tool() MCP tools: 7 read-only, plus record_session for
a caller's own remote agent to push its own data in. All thin wrappers
around metrics/store.py; importing this module is what registers them
on the shared `server` instance from mcp_server.app."""

from mcp.types import ToolAnnotations

from mcp_server.app import _log_tool_errors, current_owner, server
from metrics import store

# The seven get_* tools only read this server's own recorded data: no
# writes, no external calls, and the same args always return the same
# thing (modulo new data arriving), so repeat calls are harmless. MCP
# clients use these hints to decide what to auto-approve.
_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
# record_session writes, but only ever appends a brand-new session doc
# (never mutates or deletes an existing one), and each call mints a new
# session_id so it is not idempotent. Still a closed world -- it touches
# this server's own store, nothing external.
_APPEND_ONLY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_session_metrics(session_id: str) -> dict:
    """Full metrics for ONE recorded session: metadata plus per-prompt tokens, latency, and cost.

    Use when you have a session_id and need exact provider usage numbers for that
    session. For the block-by-block token composition of the context window use
    get_context_timeline instead; for a cost total across many sessions use
    get_cost_estimate.

    session_id: the opaque, case-sensitive id returned by record_session or listed
      by get_recent_sessions.

    Returns {"error": "session not found"} if the id is unknown or not owned by the
    caller (the two are deliberately indistinguishable).
    """
    return store.get_session_metrics(session_id, owner=current_owner.get()) or {"error": "session not found"}


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_token_breakdown(session_id: str) -> list:
    """Per-turn input/output token and latency breakdown for one session, in turn order.

    Use to see how token usage grew turn-by-turn within a session; use
    get_session_metrics for session totals.

    session_id: id from get_recent_sessions / record_session.

    Returns [] for an unknown or non-owned session.
    """
    return store.get_token_breakdown(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_tool_metrics(session_id: str | None = None) -> list:
    """Tool-call counts grouped by status (ok / error), for one session or aggregated.

    Use for a quick success/failure summary; use get_agent_trace for the ordered
    call list. Non-owners only ever see their own sessions; the owner token sees
    everyone's.

    session_id: optional. Omit for the aggregate across all your sessions; pass an
      id for just that session.
    """
    return store.get_tool_metrics(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_agent_trace(session_id: str) -> list:
    """The ordered sequence of tool calls for one session -- each entry {name, args, status}.

    Use to replay what the agent actually did, in execution order; use
    get_tool_metrics for aggregate counts.

    session_id: id from get_recent_sessions / record_session.

    Returns [] for an unknown or non-owned session.
    """
    return store.get_agent_trace(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_cost_estimate(session_id: str | None = None, period_seconds: int | None = None) -> float:
    """Estimated USD cost as a float.

    Pass session_id for one session's cost, or period_seconds for the summed cost
    of your sessions in the last N seconds. Give exactly one; non-owners are scoped
    to their own sessions. These are estimates from token counts and a static price
    table, not billed amounts.

    session_id: optional session id.
    period_seconds: optional lookback window in seconds (e.g. 86400 for the last day).

    Returns 0.0 for an unknown or non-owned session_id, and for a period with no
    matching sessions.
    """
    return store.get_cost_estimate(session_id, period_seconds, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_recent_sessions(limit: int = 10) -> list:
    """List recent sessions, newest first: [{session_id, prompt, model_id, created_at, ...}].

    Call this first to discover session_ids for the other get_* tools. Non-owners
    see only their own sessions; the owner token sees all.

    limit: max rows to return (default 10), newest first.
    """
    return store.get_recent_sessions(limit, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_context_timeline(session_id: str) -> list:
    """Ordered, categorized breakdown of everything that entered ONE session's context window.

    Each block (system prompt, tool specs, injected context, user turns, reasoning,
    tool calls/results, final answer) is marked user-visible vs invisible overhead,
    with cumulative character-based token estimates against the model's real window
    size. Use for context-window composition analysis; use get_session_metrics for
    exact provider token usage. The estimates here are character-based, not exact
    Bedrock counts.

    session_id: id from get_recent_sessions / record_session.

    Returns [] for an unknown or non-owned session, or one recorded without the
    optional context_blocks field.
    """
    return store.get_context_timeline(session_id, owner=current_owner.get())


@server.tool(annotations=_APPEND_ONLY)
@_log_tool_errors
def record_session(prompt: str, model_id: str, loop_result: dict) -> str:
    """Append ONE agent execution's metrics to this server's store; returns the new session_id.

    This is how a remote agent gets its own runs into the server, rather than only
    being able to query what the server owner recorded locally. Attributed to the
    connected identity: your Google account if you signed in via /auth/login, or
    owner=None for the owner token. NOT idempotent -- each call mints a new
    session_id. Never updates or deletes an existing session; the get_* tools read
    what this writes.

    prompt: the user prompt that started the run.
    model_id: the provider model identifier, e.g.
      "anthropic.claude-3-5-sonnet-20241022-v2:0".
    loop_result: the run's token / latency / trace payload. Required keys:
      input_tokens (int), output_tokens (int), total_tokens (int),
      latency_ms (float), turns (list). Optional: trace (list), context_blocks
      (list -- omit if you don't have per-block context data; get_context_timeline
      needs it).
      - each `turns` item: input_tokens (int), output_tokens (int),
        latency_ms (float); optional cache_read_input_tokens /
        cache_write_input_tokens (int, default 0).
      - each `trace` item: tool (str), args (dict), status (str); optional
        latency_ms (float, default 0), timestamp (float epoch seconds, default
        record time).
      - each `context_blocks` item: category (str), label (str), char_count
        (int), token_estimate (int); optional turn_n (int or null -- null for a
        pre-conversation block), status (str), content (str).

    Example loop_result:

        {
          "input_tokens": 1200, "output_tokens": 340, "total_tokens": 1540,
          "latency_ms": 4210.0,
          "turns": [
            {"input_tokens": 1200, "output_tokens": 340, "latency_ms": 4210.0,
             "cache_read_input_tokens": 800, "cache_write_input_tokens": 0}
          ],
          "trace": [
            {"tool": "grep_logs", "args": {"pattern": "ERROR"}, "status": "ok",
             "latency_ms": 120.0, "timestamp": 1756400000.0}
          ]
        }
    """
    return store.record_session(prompt, model_id, loop_result, owner=current_owner.get())
