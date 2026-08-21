"""Backend dispatcher — SQLite for local dev, DynamoDB or Firestore once
deployed. Set STORAGE_BACKEND=dynamodb (done automatically by consuming
deployments, e.g. sre-investigation-agent's AgentCore environment
variables) or STORAGE_BACKEND=firestore (this project's own Cloud Run
deployment, for durable per-user data that survives a cold start — see
docs/DEPLOYMENT.md) to switch. Same function signatures either way;
callers import from here and never know which backend is active.
"""

import os

from metrics.errors import SessionOwnershipError

_backend = os.environ.get("STORAGE_BACKEND", "sqlite")

if _backend == "dynamodb":
    from metrics.store_dynamodb import (
        append_context_block,
        append_tool_call,
        append_turn,
        close_session,
        get_agent_trace,
        get_context_timeline,
        get_cost_estimate,
        get_recent_sessions,
        get_session_metrics,
        get_token_breakdown,
        get_tool_metrics,
        record_session,
        start_or_get_session,
    )
elif _backend == "firestore":
    from metrics.store_firestore import (
        append_context_block,
        append_tool_call,
        append_turn,
        close_session,
        get_agent_trace,
        get_context_timeline,
        get_cost_estimate,
        get_recent_sessions,
        get_session_metrics,
        get_token_breakdown,
        get_tool_metrics,
        record_session,
        start_or_get_session,
    )
else:
    from metrics.store_sqlite import (
        append_context_block,
        append_tool_call,
        append_turn,
        close_session,
        get_agent_trace,
        get_context_timeline,
        get_cost_estimate,
        get_recent_sessions,
        get_session_metrics,
        get_token_breakdown,
        get_tool_metrics,
        record_session,
        start_or_get_session,
    )

__all__ = [
    "record_session",
    "get_session_metrics",
    "get_token_breakdown",
    "get_tool_metrics",
    "get_agent_trace",
    "get_cost_estimate",
    "get_recent_sessions",
    "get_context_timeline",
    "start_or_get_session",
    "append_turn",
    "append_tool_call",
    "append_context_block",
    "close_session",
    "SessionOwnershipError",
]
