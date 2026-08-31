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
    """Session metadata plus per-prompt metrics (tokens, latency, cost) for one investigation."""
    return store.get_session_metrics(session_id, owner=current_owner.get()) or {"error": "session not found"}


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_token_breakdown(session_id: str) -> list:
    """Per-turn token and latency breakdown for one session."""
    return store.get_token_breakdown(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_tool_metrics(session_id: str | None = None) -> list:
    """Tool call counts by status, for one session or aggregated across all (aggregated across
    only your own sessions if you're not the server owner)."""
    return store.get_tool_metrics(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_agent_trace(session_id: str) -> list:
    """The ordered sequence of tool calls (name, args, status) for one session."""
    return store.get_agent_trace(session_id, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_cost_estimate(session_id: str | None = None, period_seconds: int | None = None) -> float:
    """Estimated cost for one session, or summed over the last period_seconds (your own
    sessions only, unless you're the server owner)."""
    return store.get_cost_estimate(session_id, period_seconds, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_recent_sessions(limit: int = 10) -> list:
    """The most recent investigation sessions, newest first. Your own only, unless
    you're the server owner (owner token), who sees everyone's."""
    return store.get_recent_sessions(limit, owner=current_owner.get())


@server.tool(annotations=_READ_ONLY)
@_log_tool_errors
def get_context_timeline(session_id: str) -> list:
    """Ordered, categorized breakdown of everything that entered this
    session's context window, with cumulative token estimates. These are
    character-based estimates for composition, not exact Bedrock usage
    numbers (see get_session_metrics for those)."""
    return store.get_context_timeline(session_id, owner=current_owner.get())


@server.tool(annotations=_APPEND_ONLY)
@_log_tool_errors
def record_session(prompt: str, model_id: str, loop_result: dict) -> str:
    """Records one agent execution's metrics, attributed to whoever's
    connected (your own Google account, if you signed in via
    /auth/login; the owner token records as owner=None, same as the
    local direct-import path `from metrics.store import record_session`).
    This is how a caller's own remote agent gets its sessions into this
    server at all, rather than only being able to query data the server
    owner recorded locally.

    loop_result must have exactly this shape:

        {
          "input_tokens": int,
          "output_tokens": int,
          "total_tokens": int,
          "latency_ms": float,
          "turns": [
            {"input_tokens": int, "output_tokens": int, "latency_ms": float,
             "cache_read_input_tokens": int,   # optional, defaults 0
             "cache_write_input_tokens": int}, # optional, defaults 0
            ...
          ],
          "trace": [
            {"tool": str, "args": dict, "status": str,
             "latency_ms": float,   # optional, defaults 0
             "timestamp": float},   # optional, defaults to record time
            ...
          ],
          "context_blocks": [...]  # optional, omit if you don't have it
        }

    Returns the new session_id."""
    return store.record_session(prompt, model_id, loop_result, owner=current_owner.get())
