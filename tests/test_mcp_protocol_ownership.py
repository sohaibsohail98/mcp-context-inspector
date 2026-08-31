"""End-to-end proof that per-owner data isolation actually works over a
real MCP JSON-RPC handshake (initialize -> notifications/initialized ->
tools/call), not just at the metrics/store.py layer. Confirms
MultiTokenAuthMiddleware's `current_owner.set(...)` (called before
`call_next()`) stays visible by the time the MCP SDK dispatches the
tools/call to our tool functions. Starlette's BaseHTTPMiddleware runs
the downstream ASGI app in the same asyncio task, so a contextvar set
here propagates through to tool dispatch.

In-process via Starlette's TestClient (isolated DB fixtures, no real
subprocess, no real network). The raw JSON-RPC wire format mirrors the
a browser-side MCP client, a reference
implementation of this protocol, rather than depending on the `mcp`
SDK's own client internals.
"""

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module


@pytest.fixture
def client(isolated_auth_store, isolated_sqlite_db):
    app = server_module.server.streamable_http_app()
    app.add_middleware(server_module.MultiTokenAuthMiddleware, owner_token="owner-secret")
    # base_url must include an explicit port. The MCP SDK's transport
    # security (DNS-rebinding protection) only allow-lists "127.0.0.1:*"
    # (a port required), and rejects TestClient's default "testserver"
    # Host header, and a bare "127.0.0.1" with no port, both with a 421.
    with TestClient(app, base_url="http://127.0.0.1:8787") as c:
        yield c


def _parse_response(resp):
    """Response body is either a plain JSON object or an SSE stream of
    "data: {...}" frames, the same dual-format handling as
    a browser client's _parseSseResponse, the reference implementation
    for this wire format."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return resp.json()
    last = None
    for frame in resp.text.replace("\r\n", "\n").split("\n\n"):
        for line in frame.split("\n"):
            if line.startswith("data: "):
                import json

                last = json.loads(line[len("data: ") :])
    return last


class RawMcpClient:
    """Minimal raw Streamable-HTTP JSON-RPC client: just enough of the
    protocol to call tools with a given bearer token, mirroring
    a browser client's McpClient."""

    def __init__(self, http_client, token):
        self.http_client = http_client
        self.token = token
        self.session_id = None
        self._next_id = 1

    def _send(self, body):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        resp = self.http_client.post("/mcp", json=body, headers=headers)
        assert resp.status_code < 400, f"MCP request failed: {resp.status_code} {resp.text}"
        returned_session_id = resp.headers.get("mcp-session-id")
        if returned_session_id:
            self.session_id = returned_session_id
        if resp.status_code == 202:
            return None
        return _parse_response(resp)

    def initialize(self):
        init_response = self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest-raw-client", "version": "1.0.0"},
                },
            }
        )
        self._next_id += 1
        assert "error" not in init_response, init_response
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return init_response["result"]

    def call_tool(self, name, arguments):
        """Returns the tool's actual return value, unwrapped. Same
        wire-format handling as a browser client's callTool(): a scalar
        return (e.g. record_session's str) carries structuredContent
        {"result": value}; a list/dict return has no structuredContent,
        each JSON-parsed content block being one list item (or the
        single dict, for a dict-returning tool)."""
        response = self._send(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        self._next_id += 1
        assert "error" not in response, response
        result = response["result"]
        assert result.get("isError") is not True, result

        structured = result.get("structuredContent")
        if structured and set(structured.keys()) == {"result"}:
            return structured["result"]

        import json

        return [json.loads(block["text"]) for block in result["content"]]


def _basic_loop_result():
    return {
        "trace": [],
        "turns": [{"input_tokens": 10, "output_tokens": 5, "latency_ms": 100}],
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "latency_ms": 100,
    }


def test_owner_token_record_and_read_is_unscoped(client):
    mcp = RawMcpClient(client, "owner-secret")
    mcp.initialize()
    mcp.call_tool("record_session", {"prompt": "owner's q", "model_id": "m", "loop_result": _basic_loop_result()})
    result = mcp.call_tool("get_recent_sessions", {"limit": 10})
    assert len(result) == 1


def test_per_user_token_writes_and_reads_are_scoped_to_that_user(client, isolated_auth_store):
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    alice = RawMcpClient(client, alice_token)
    alice.initialize()
    alice.call_tool("record_session", {"prompt": "alice's q", "model_id": "m", "loop_result": _basic_loop_result()})

    bob = RawMcpClient(client, bob_token)
    bob.initialize()
    bob.call_tool("record_session", {"prompt": "bob's q", "model_id": "m", "loop_result": _basic_loop_result()})

    # The real assertion: this is a genuine MCP tools/call dispatch, over
    # a real initialize handshake, on two separate sessions. If the
    # contextvar didn't survive from middleware to tool dispatch, both
    # would either see everything or see nothing, not exactly their own.
    alice_sessions = alice.call_tool("get_recent_sessions", {"limit": 10})
    assert len(alice_sessions) == 1
    assert alice_sessions[0]["prompt"] == "alice's q"

    bob_sessions = bob.call_tool("get_recent_sessions", {"limit": 10})
    assert len(bob_sessions) == 1
    assert bob_sessions[0]["prompt"] == "bob's q"

    owner = RawMcpClient(client, "owner-secret")
    owner.initialize()
    all_sessions = owner.call_tool("get_recent_sessions", {"limit": 10})
    assert len(all_sessions) == 2


def test_anon_client_can_discover_tools_but_not_read_data(client, isolated_auth_store):
    """A registry crawler with NO bearer token (e.g. Glama's build-test
    inspector) can run the discovery handshake and enumerate the 8 tools,
    but any data tools/call is still 401. See
    MultiTokenAuthMiddleware._MCP_DATA_METHODS."""
    # Seed one real session as the owner so "sees no data" is a
    # meaningful assertion, not just an empty store.
    owner = RawMcpClient(client, "owner-secret")
    owner.initialize()
    owner.call_tool("record_session", {"prompt": "owner's q", "model_id": "m", "loop_result": _basic_loop_result()})

    # Anonymous: no Authorization header at all.
    init = client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "anon", "version": "1"}},
        },
    )
    assert init.status_code == 200, init.text
    sid = init.headers["mcp-session-id"]
    client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Session-Id": sid},
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    listed = client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Session-Id": sid},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    assert listed.status_code == 200, listed.text
    tools = {t["name"] for t in _parse_response(listed)["result"]["tools"]}
    assert len(tools) == 8
    assert "record_session" in tools and "get_recent_sessions" in tools

    # But a data call with no token is refused at the auth gate.
    denied = client.post(
        "/mcp",
        headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Session-Id": sid},
        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_recent_sessions", "arguments": {}}},
    )
    assert denied.status_code == 401


def test_per_user_token_cannot_read_another_users_session_by_id(client, isolated_auth_store):
    alice_token = isolated_auth_store.get_or_create_token("alice-sub", "alice@example.com")
    bob_token = isolated_auth_store.get_or_create_token("bob-sub", "bob@example.com")

    alice = RawMcpClient(client, alice_token)
    alice.initialize()
    session_id = alice.call_tool(
        "record_session", {"prompt": "alice's private q", "model_id": "m", "loop_result": _basic_loop_result()}
    )

    bob = RawMcpClient(client, bob_token)
    bob.initialize()
    # get_session_metrics returns a dict: one content block, unwrapped
    # from the generic list-of-blocks parsing the same way
    # a browser client's callers do (`const [metrics] = await ...`).
    [metrics] = bob.call_tool("get_session_metrics", {"session_id": session_id})
    # {"error": "session not found"} for a session that either doesn't
    # exist or isn't yours: indistinguishable, by design (see
    # metrics/store_sqlite.py's get_session_metrics docstring).
    assert metrics["error"] == "session not found"
