"""OTLP telemetry ingestion — Claude Code's and GitHub Copilot's own
native OpenTelemetry export, sent here instead of through the MCP
connection (see docs/internal/OTLP_INTEGRATION_PLAN.md's "Why" section: MCP
only ever sees calls made to our own tools, never a client's own
token usage). Gated by MultiTokenAuthMiddleware exactly like /api/ —
"/otlp" is in its protected_prefixes, same bearer-token-to-owner
mapping, set via OTEL_EXPORTER_OTLP_HEADERS on the client side. Body
parsing and per-vendor mapping lives in mcp_server/otlp/."""

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_server import otlp
from mcp_server.app import current_owner, server


# Raw request/response bodies can legitimately be large (full source
# files, long conversations), but with no cap at all a single POST can
# force the server to buffer an unbounded amount of memory before any
# per-item processing/limiting happens — found in review. 25MB is well
# above any single real OTLP batch (Claude Code's own inline-body mode
# truncates far below this) and still a hard ceiling against abuse.
_MAX_OTLP_BODY_BYTES = 25 * 1024 * 1024


async def _otlp_body(request: Request):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_OTLP_BODY_BYTES:
                return "too_large"
        except ValueError:
            pass
    raw = await request.body()
    if len(raw) > _MAX_OTLP_BODY_BYTES:
        return "too_large"
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


@server.custom_route("/otlp/v1/logs", methods=["POST"])
async def otlp_logs(request: Request):
    body = await _otlp_body(request)
    if body == "too_large":
        return JSONResponse({"error": "request body too large"}, status_code=413)
    if body is None:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    counts = otlp.handle_logs_payload(body, owner=current_owner.get())
    return JSONResponse({"accepted": counts})


@server.custom_route("/otlp/v1/metrics", methods=["POST"])
async def otlp_metrics(request: Request):
    body = await _otlp_body(request)
    if body == "too_large":
        return JSONResponse({"error": "request body too large"}, status_code=413)
    if body is None:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    counts = otlp.handle_metrics_payload(body, owner=current_owner.get())
    return JSONResponse({"accepted": counts})


@server.custom_route("/otlp/v1/traces", methods=["POST"])
async def otlp_traces(request: Request):
    body = await _otlp_body(request)
    if body == "too_large":
        return JSONResponse({"error": "request body too large"}, status_code=413)
    if body is None:
        return JSONResponse({"error": "malformed JSON body"}, status_code=400)
    counts = otlp.handle_traces_payload(body, owner=current_owner.get())
    return JSONResponse({"accepted": counts})


@server.custom_route("/otlp/debug", methods=["GET"])
async def otlp_debug(request: Request):
    """Troubleshooting aid, not a metrics endpoint — answers "did MY
    data reach this server, and what did it think it was," which a
    fire-and-forget OTel exporter otherwise gives zero visibility into
    from the client side. In-memory only (see mcp_server/otlp/__init__.py's
    _counts) — resets on every redeploy/restart, so a zero here after a
    fresh deploy doesn't by itself mean nothing has arrived since.

    Scoped to current_owner: each caller only ever sees their own
    counters and recent_skipped entries. Found in review — this used to
    return process-global counters, which both false-positived
    "connected" across tenants and leaked other tenants' resource_attrs
    (hostnames, session IDs) to any authenticated caller."""
    return JSONResponse(
        {
            "message": "in-memory counters since last server restart/redeploy, scoped to your account",
            **otlp.debug_snapshot(current_owner.get()),
        }
    )
