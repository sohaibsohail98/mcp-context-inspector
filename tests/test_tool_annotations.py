"""Every MCP tool must advertise explicit read-only / destructive /
idempotent / open-world hints, so a client can decide what to
auto-approve. The seven get_* tools are pure reads of this server's own
store; record_session is an append (writes, but only ever adds a new
session doc, and mints a fresh id each call so it is not idempotent).
Neither talks to anything outside this server, so open_world is always
false. Locks in the values set in mcp_server/tools.py.
"""

import asyncio

import mcp_server.tools  # noqa: F401  -- import registers the tools onto `server`
from mcp_server.app import server

_READ_ONLY_TOOLS = {
    "get_session_metrics",
    "get_token_breakdown",
    "get_tool_metrics",
    "get_agent_trace",
    "get_cost_estimate",
    "get_recent_sessions",
    "get_context_timeline",
}
_APPEND_TOOLS = {"record_session"}


def _tools_by_name():
    tools = asyncio.run(server.list_tools())
    return {t.name: t for t in tools}


def test_all_tools_carry_all_four_hints():
    for name, tool in _tools_by_name().items():
        a = tool.annotations
        assert a is not None, f"{name} has no annotations"
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert getattr(a, hint) is not None, f"{name}.{hint} is unset"


def test_read_only_tools_are_marked_read_only():
    by_name = _tools_by_name()
    assert _READ_ONLY_TOOLS <= set(by_name), "expected read-only tools missing from the registry"
    for name in _READ_ONLY_TOOLS:
        a = by_name[name].annotations
        assert a.read_only_hint is True
        assert a.destructive_hint is False
        assert a.idempotent_hint is True
        assert a.open_world_hint is False


def test_record_session_is_append_only_not_idempotent():
    a = _tools_by_name()["record_session"].annotations
    assert a.read_only_hint is False
    assert a.destructive_hint is False  # additive: only ever inserts a new session
    assert a.idempotent_hint is False  # new session_id per call
    assert a.open_world_hint is False


def test_registry_is_exactly_the_eight_known_tools():
    assert set(_tools_by_name()) == _READ_ONLY_TOOLS | _APPEND_TOOLS
