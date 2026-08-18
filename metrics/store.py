"""Backend dispatcher — SQLite for local dev, DynamoDB once deployed.
Set STORAGE_BACKEND=dynamodb (done automatically in the AgentCore
deployment's environment_variables — see terraform/main.tf) to switch.
Same function signatures either way; app.py and mcp_server/server.py
import from here and never know which backend is active.
"""

import os

if os.environ.get("STORAGE_BACKEND", "sqlite") == "dynamodb":
    from metrics.store_dynamodb import (
        get_agent_trace,
        get_context_timeline,
        get_cost_estimate,
        get_recent_sessions,
        get_session_metrics,
        get_token_breakdown,
        get_tool_metrics,
        record_session,
    )
else:
    from metrics.store_sqlite import (
        get_agent_trace,
        get_context_timeline,
        get_cost_estimate,
        get_recent_sessions,
        get_session_metrics,
        get_token_breakdown,
        get_tool_metrics,
        record_session,
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
]
