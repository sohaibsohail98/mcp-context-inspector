"""Server-wide state: the MCPServer instance every tool/route module
registers onto, the per-request `current_owner` contextvar, and the
tool-error-logging decorator. Has no route or tool definitions of its
own, so other mcp_server modules can import `server`/`current_owner`
without a circular import."""

import contextvars
import functools
import logging

from mcp.server.mcpserver import MCPServer

server = MCPServer(name="sre-agent-metrics")

logger = logging.getLogger(__name__)


def _log_tool_errors(fn):
    """MCP serializes a tool exception straight into the JSON-RPC error
    response — it never reaches Cloud Run's stdout/stderr on its own, so a
    failure like a locked SQLite file is otherwise invisible server-side.
    Logs, then re-raises unchanged so MCP's own error handling still runs."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception:
            logger.exception("MCP tool %s failed", fn.__name__)
            raise

    return wrapper

# Set by MultiTokenAuthMiddleware once per request: the caller's Google
# `sub` if they connected with a per-user token, or None if they used the
# owner token (which sees everything). A plain contextvar rather than a
# function parameter because Starlette's BaseHTTPMiddleware runs the
# downstream handler in the same asyncio task, so a value set here before
# call_next() stays visible through request handling, including MCP tool
# dispatch (see tests/test_mcp_protocol_ownership.py).
current_owner = contextvars.ContextVar("current_owner", default=None)
