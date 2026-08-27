"""OTLP telemetry ingestion. Receives Claude Code's and GitHub
Copilot's native OpenTelemetry export (JSON protocol, `http/json`) and
maps it onto the same session/turn/tool_call/context_block schema
metrics/store.py uses for the Bedrock agent's record_session().

Dispatch is by OTLP resource attributes (`service.name` and friends).
The exact attribute value each vendor stamps has not been verified
against a real captured payload. If real payloads don't match, extend
detect_vendor's checks rather than assuming the receiver route itself
is wrong.
"""

import logging
import time
from collections import defaultdict, deque

from mcp_server.otlp import claude_code, copilot
from mcp_server.otlp.common import resource_attrs_dict

logger = logging.getLogger(__name__)

# In-memory only (resets on redeploy/restart). Backs GET /otlp/debug,
# which answers "was anything received at all, and what did the server
# think it was" for a fire-and-forget OTel exporter. Unlike
# metrics/store.py this also records *rejected* data.
#
# Keyed by owner (the google_sub current_owner resolves to, or the
# sentinel _OWNER_TOKEN_KEY for the shared owner-token caller). This used
# to be flat process-global state, which both false-positived
# "connected" for any signed-in caller the instant ANY tenant's data
# landed, and leaked other tenants' resource_attrs (hostnames, session
# IDs) via recent_skipped to any authenticated caller.
_OWNER_TOKEN_KEY = "__owner_token__"


def _owner_key(owner):
    return _OWNER_TOKEN_KEY if owner is None else owner


def _new_counts():
    return {"claude_code": 0, "copilot": 0, "skipped": 0}


_counts = defaultdict(_new_counts)
_last_accepted_at = defaultdict(lambda: {"claude_code": None, "copilot": None})
_recent_skipped = defaultdict(lambda: deque(maxlen=20))


def _record_skipped(owner, resource_attrs, record_count=1):
    key = _owner_key(owner)
    _counts[key]["skipped"] += record_count
    _recent_skipped[key].append({"at": time.time(), "resource_attrs": resource_attrs})


def _record_accepted(owner, vendor):
    key = _owner_key(owner)
    _counts[key][vendor] += 1
    _last_accepted_at[key][vendor] = time.time()


def debug_snapshot(owner):
    """Plain-dict snapshot for the /otlp/debug route (see
    mcp_server/routes/otlp.py). Scoped to `owner` (None = the shared
    owner-token's own all-data counters, which are their own bucket here,
    not literally every tenant's data merged; see _owner_key)."""
    key = _owner_key(owner)
    return {
        "counts": dict(_counts[key]),
        "last_accepted_at": dict(_last_accepted_at[key]),
        "recent_skipped": list(_recent_skipped[key]),
    }


# Candidate service.name values for each vendor. Claude Code's telemetry
# docs don't spell out the exact resource attribute value; "claude-code"
# is the reasonable default given every other Claude Code env var/metric
# is namespaced `claude_code.*` / `claude-code`. Verify and extend this
# set once a real payload has been captured locally.
_CLAUDE_CODE_SERVICE_NAMES = {"claude-code", "claude_code"}
_COPILOT_SERVICE_NAMES = {"github-copilot", "copilot", "github.copilot"}


def _log_undetected_vendor(resource_attrs):
    """An undetected vendor is otherwise completely silent: Claude Code's
    OTLP exporter fires-and-forgets, so the "skipped" count in the
    response body is never seen. Logging it (plus _record_skipped for GET
    /otlp/debug) is what makes a mismatched payload's real resource
    attribute shape readable without Cloud Run log access. resource_attrs
    is OTLP *resource* metadata only (service name, session id, host
    info), never prompt/response content, so logging it in full is safe."""
    logger.warning("otlp: undetected vendor, resource_attrs=%r", resource_attrs)


def detect_vendor(resource_attrs):
    """resource_attrs: plain dict (already run through
    resource_attrs_dict). Returns "claude_code", "copilot", or None.
    Falls back to sniffing event/attribute-name shape when service.name
    doesn't match a known value outright. Claude Code's raw bodies
    carry event.name values like api_request_body/api_response_body;
    Copilot's carry gen_ai.* keys. Cheap and specific enough not to
    false-positive between the two."""
    service_name = str(resource_attrs.get("service.name", "")).lower()
    if service_name in _CLAUDE_CODE_SERVICE_NAMES:
        return "claude_code"
    if service_name in _COPILOT_SERVICE_NAMES:
        return "copilot"
    if any(k.startswith("gen_ai.") for k in resource_attrs):
        return "copilot"
    if "session.id" in resource_attrs or "claude_code.version" in resource_attrs:
        return "claude_code"
    return None


def handle_logs_payload(payload, owner):
    """payload: parsed OTLP JSON body of a POST to /otlp/v1/logs
    (`{"resourceLogs": [...]}`). Routes each resourceLogs entry to the
    matching vendor mapper's handle_logs; entries from an
    unrecognized vendor are counted as skipped, not dropped silently.
    The receiver route surfaces the count in its response so a
    misconfigured client is visible in its own terminal output.
    Returns {"vendor": count, ..., "skipped": count}."""
    counts = {"claude_code": 0, "copilot": 0, "skipped": 0}
    for resource_logs in payload.get("resourceLogs", []):
        attrs = resource_attrs_dict(resource_logs.get("resource", {}))
        vendor = detect_vendor(attrs)
        log_records = [
            record for scope_logs in resource_logs.get("scopeLogs", []) for record in scope_logs.get("logRecords", [])
        ]
        if vendor == "claude_code":
            claude_code.handle_logs(attrs, log_records, owner)
            counts["claude_code"] += len(log_records)
            _record_accepted(owner, "claude_code")
        elif vendor == "copilot":
            copilot.handle_logs(attrs, log_records, owner)
            counts["copilot"] += len(log_records)
            _record_accepted(owner, "copilot")
        else:
            _log_undetected_vendor(attrs)
            _record_skipped(owner, attrs, len(log_records))
            counts["skipped"] += len(log_records)
    return counts


def handle_metrics_payload(payload, owner):
    """payload: parsed OTLP JSON body of a POST to /otlp/v1/metrics
    (`{"resourceMetrics": [...]}`)."""
    counts = {"claude_code": 0, "copilot": 0, "skipped": 0}
    for resource_metrics in payload.get("resourceMetrics", []):
        attrs = resource_attrs_dict(resource_metrics.get("resource", {}))
        vendor = detect_vendor(attrs)
        metrics = [
            metric
            for scope_metrics in resource_metrics.get("scopeMetrics", [])
            for metric in scope_metrics.get("metrics", [])
        ]
        if vendor == "claude_code":
            claude_code.handle_metrics(attrs, metrics, owner)
            counts["claude_code"] += len(metrics)
            _record_accepted(owner, "claude_code")
        elif vendor == "copilot":
            copilot.handle_metrics(attrs, metrics, owner)
            counts["copilot"] += len(metrics)
            _record_accepted(owner, "copilot")
        else:
            _log_undetected_vendor(attrs)
            _record_skipped(owner, attrs, len(metrics))
            counts["skipped"] += len(metrics)
    return counts


def handle_traces_payload(payload, owner):
    """payload: parsed OTLP JSON body of a POST to /otlp/v1/traces
    (`{"resourceSpans": [...]}`). Only Copilot's mapper uses trace spans
    today (tool-call attribution, including MCP-sourced calls). Claude
    Code's base v1 mapper works entirely off log bodies/metrics; its own
    trace-span join is deferred past v1."""
    counts = {"claude_code": 0, "copilot": 0, "skipped": 0}
    for resource_spans in payload.get("resourceSpans", []):
        attrs = resource_attrs_dict(resource_spans.get("resource", {}))
        vendor = detect_vendor(attrs)
        spans = [span for scope_spans in resource_spans.get("scopeSpans", []) for span in scope_spans.get("spans", [])]
        if vendor == "copilot":
            copilot.handle_traces(attrs, spans, owner)
            counts["copilot"] += len(spans)
            _record_accepted(owner, "copilot")
        else:
            _log_undetected_vendor(attrs)
            _record_skipped(owner, attrs, len(spans))
            counts["skipped"] += len(spans)
    return counts
