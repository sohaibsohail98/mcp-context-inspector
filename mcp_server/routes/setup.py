"""/setup/* — writes this account's MCP connection + OTLP config into
the caller's own ~/.claude/settings.json, either directly (loopback-only,
self-hosted case) or via a downloadable personalized script (deployed
case, see mcp_server/local_setup.py for the shared merge/backup logic
both paths call)."""

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from mcp_server import local_setup
from mcp_server.app import server
from mcp_server.middleware import _public_origin


def _is_loopback_request(request):
    host = request.client.host if request.client else None
    return host in ("127.0.0.1", "::1")


@server.custom_route("/setup/apply-local-config", methods=["POST"])
async def apply_local_config(request: Request):
    """Merges this session's MCP server entry + OTLP telemetry env vars
    into the caller's own ~/.claude/settings.json — the same shape the
    connect page's copy-paste snippets already produce, applied for them
    instead of by them. Backs up the existing file first (never a plain
    overwrite); merges into (never replaces) existing mcpServers/env keys,
    so an existing entry for a different MCP server (or a different env
    var) survives untouched."""
    if not _is_loopback_request(request):
        return JSONResponse(
            {"error": "only available when this server and your browser are on the same machine"},
            status_code=403,
        )

    auth_header = request.headers.get("authorization", "")
    bearer_token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
    if not bearer_token:
        return JSONResponse({"error": "missing bearer token"}, status_code=401)

    base = str(request.base_url)
    patch = local_setup.build_settings_patch(base, bearer_token)
    try:
        backed_up_to, written_path = local_setup.apply_settings_patch(patch, local_setup.SETTINGS_PATH)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    return JSONResponse({
        "ok": True,
        "path": written_path,
        "backed_up_to": backed_up_to,
    })


@server.custom_route("/setup/local-script", methods=["GET"])
async def local_script(request: Request):
    """Returns a personalized, standalone Python script that performs the
    exact same settings.json merge as /setup/apply-local-config above, for
    a caller whose browser and server aren't on the same machine (i.e. a
    deployed instance). The script is generated per-request from an
    authenticated call, never a static link, so a leaked URL alone
    (browser history, a proxy log) can't hand out a live token —
    MultiTokenAuthMiddleware already enforces the bearer-token check via
    protected_prefixes before this handler runs."""
    auth_header = request.headers.get("authorization", "")
    bearer_token = auth_header.removeprefix("Bearer ") if auth_header.startswith("Bearer ") else None
    if not bearer_token:
        return JSONResponse({"error": "missing bearer token"}, status_code=401)

    # Unlike apply_local_config above, this route is reachable from a
    # deployed instance — request.base_url would be wrong there (see
    # _public_origin's docstring), so this one can't just use it directly.
    base = _public_origin(request)
    script = local_setup.render_local_script(base, bearer_token)
    return PlainTextResponse(
        script,
        headers={"Content-Disposition": 'attachment; filename="mcp-context-inspector-setup.py"'},
    )


