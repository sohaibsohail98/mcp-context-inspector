"""Plain REST routes, a curl-friendly alternative to a real MCP
handshake. Same underlying metrics/store.py functions the MCP tools in
tools.py call; one data-access layer, not two implementations of "how do
I read a session." Importing this module registers these routes on the
shared `server` instance."""

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from metrics import store

from mcp_server import dev_mode
from mcp_server.app import current_owner, server


@server.custom_route("/api/sessions", methods=["GET"])
async def api_sessions(request: Request):
    limit = int(request.query_params.get("limit", 10))
    owner = current_owner.get()
    # ?include_test_sessions=1 only takes effect for an allowlisted
    # account (see dev_mode.py). Anyone else's request for it is
    # silently ignored rather than erroring, so a stray query param
    # never becomes a way to probe who's on the allowlist.
    include_test_sessions = (
        request.query_params.get("include_test_sessions") == "1" and dev_mode.is_dev_mode_account(owner)
    )
    return JSONResponse(store.get_recent_sessions(limit, owner=owner, include_test_sessions=include_test_sessions))


@server.custom_route("/api/dev-mode-status", methods=["GET"])
async def api_dev_mode_status(request: Request):
    """Tells the dashboard whether to render the "show test sessions"
    toggle at all. The toggle itself is meaningless to anyone not on
    the DEV_MODE_SUBS allowlist, so it's hidden rather than shown-and-
    disabled for everyone else."""
    return JSONResponse({"dev_mode": dev_mode.is_dev_mode_account(current_owner.get())})


@server.custom_route("/api/sessions/{session_id}", methods=["GET"])
async def api_session_detail(request: Request):
    session_id = request.path_params["session_id"]
    owner = current_owner.get()
    metrics = store.get_session_metrics(session_id, owner=owner)
    if metrics is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "metrics": metrics,
            "turns": store.get_token_breakdown(session_id, owner=owner),
            "trace": store.get_agent_trace(session_id, owner=owner),
        }
    )


@server.custom_route("/api/tool-metrics", methods=["GET"])
async def api_tool_metrics(request: Request):
    return JSONResponse(store.get_tool_metrics(owner=current_owner.get()))


@server.custom_route("/api/cost", methods=["GET"])
async def api_cost(request: Request):
    period = request.query_params.get("period_seconds")
    period = int(period) if period else None
    return JSONResponse({"total_cost": store.get_cost_estimate(period_seconds=period, owner=current_owner.get())})


@server.custom_route("/api/context-timeline/{session_id}", methods=["GET"])
async def api_context_timeline(request: Request):
    session_id = request.path_params["session_id"]
    return JSONResponse(store.get_context_timeline(session_id, owner=current_owner.get()))


@server.custom_route("/", methods=["GET"])
async def root_redirect(request: Request):
    """This is a headless MCP server, not a browsable app. Visiting the
    bare URL with no route registered here would otherwise 404 with a
    blank page, which reads as "broken" to anyone who just opens the
    service URL. /auth/login is the actual human entry point (sign-in +
    connection instructions)."""
    return RedirectResponse(url="/auth/login")


@server.custom_route("/health", methods=["GET"])
async def healthz(request: Request):
    """Unauthenticated. Used by Cloud Scheduler to keep the deployed
    instance warm, and by anyone checking the service is up at all."""
    return JSONResponse({"status": "ok"})


@server.custom_route("/api/record-session", methods=["POST"])
async def api_record_session(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    try:
        prompt, model_id, loop_result = body["prompt"], body["model_id"], body["loop_result"]
    except KeyError as e:
        return JSONResponse({"error": f"missing field: {e}"}, status_code=400)
    session_id = store.record_session(prompt, model_id, loop_result, owner=current_owner.get())
    return JSONResponse({"session_id": session_id})
