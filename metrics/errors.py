"""Shared exception types for metrics/store.py's two backends.

A tiny standalone module, not defined inside store_sqlite.py/
store_dynamodb.py themselves, so mcp_server/otlp/*.py can import and
catch it without caring which backend is active.
"""


class SessionOwnershipError(Exception):
    """Raised when a caller tries to write into a session_id that
    already belongs to a different owner. OTLP session_ids are
    client-chosen (Claude Code's/Copilot's own IDs), not
    server-generated uuid4s like record_session's. A caller holding a
    valid-but-unprivileged per-user token could otherwise pick another
    user's existing session_id and inject fabricated tool calls or
    token totals into it. Every append_*/close_session/
    start_or_get_session call must check ownership before writing, the
    same way every read already does via _visible()."""
