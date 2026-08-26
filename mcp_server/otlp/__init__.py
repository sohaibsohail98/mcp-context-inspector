"""OTLP telemetry ingestion — receives Claude Code's and GitHub
Copilot's native OpenTelemetry export (JSON protocol, `http/json`) and
maps it onto the same session/turn/tool_call/context_block schema
metrics/store.py uses for the Bedrock agent's record_session(). See
docs/internal/OTLP_INTEGRATION_PLAN.md for the underlying research.

Dispatch is by OTLP resource attributes (`service.name` and friends).
The exact attribute value each vendor stamps has not been verified
against a real captured payload — if real payloads don't match, extend
_detect_vendor's checks rather than assuming the receiver route itself
is wrong.
"""

import logging
import time
from collections import defaultdict, deque

from mcp_server.otlp import claude_code, copilot
from mcp_server.otlp.common import resource_attrs_dict

logger = logging.getLogger(__name__)

# In-memory only (resets on redeploy/restart) — this backs GET /otlp/debug,
# a troubleshooting aid for exactly the "was anything received at all, and
# what did the server think it was" question a fire-and-forget OTel
# exporter otherwise gives no visibility into. Not a metrics/analytics
# system (metrics/store.py + Firestore/DynamoDB already do that for
# accepted data) — this exists specifically to see *rejected* data too.
#
# Keyed by owner (the google_sub current_owner resolves to, or the
# sentinel _OWNER_TOKEN_KEY for the shared owner-token/all-data caller —
# see current_owner's docstring) so GET /otlp/debug can answer "did MY
# data arrive" instead of leaking every tenant's counters to whoever asks
# first. Found in review: this used to be flat process-global state,
# which both false-positived "connected" for any signed-in caller the
# instant ANY tenant's data landed, and leaked other tenants' resource_attrs
# (hostnames, session IDs) via recent_skipped to any authenticated caller.
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
    """Plain-dict snapshot for the /otlp/debug route — see
    mcp_server/routes/otlp.py. Scoped to `owner` (None = the shared
    owner-token's own all-data counters, which are their own bucket here,
    not literally every tenant's data merged — see _owner_key)."""
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
    """A real client whose resource attributes don't match any of
    detect_vendor's checks gets counted as "skipped" in the response
    body, which is only visible if something is actually looking at
    that response — Claude Code's own OTLP exporter fires-and-forgets
    and prints nothing. Without this, an undetected vendor is
    completely silent: no error, no 4xx, no visible signal anywhere
    that data arrived and was thrown away. Logging it — and recording it
    via _record_skipped for GET /otlp/debug — lets a real payload's
    actual resource attribute shape be read back without needing Cloud
    Run log access, which is exactly the missing piece that made this
    bug unfalsifiable from outside the server (see api_tests/README.md
    and the investigation report — no real captured payload was ever
    persisted anywhere in this repo). resource_attrs is OTLP *resource*
    metadata (service name, session id, host info) — never prompt/
    response content, which lives in log_records/metrics/spans and is
    never passed here — so logging/storing it in full is safe."""
    logger.warning("otlp: undetected vendor, resource_attrs=%r", resource_attrs)


def detect_vendor(resource_attrs):
    """resource_attrs: plain dict (already run through
    resource_attrs_dict). Returns "claude_code", "copilot", or None.
    Falls back to sniffing event/attribute-name shape when service.name
    doesn't match a known value outright — Claude Code's raw bodies
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
    unrecognized vendor are counted as skipped, not dropped silently —
    the receiver route surfaces the count in its response so a
    misconfigured client is visible in its own terminal output.
    Returns {"vendor": count, ..., "skipped": count}."""
    counts = {"claude_code": 0, "copilot": 0, "skipped": 0}
    for resource_logs in payload.get("resourceLogs", []):
        attrs = resource_attrs_dict(resource_logs.get("resource", {}))
        vendor = detect_vendor(attrs)
        log_records = [
            record
            for scope_logs in resource_logs.get("scopeLogs", [])
            for record in scope_logs.get("logRecords", [])
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
    today (tool-call attribution, including MCP-sourced calls) — Claude
    Code's base v1 mapper works entirely off log bodies/metrics (see the
    plan's "Subagent context-window granularity" section on why its own
    trace-span join is deferred past v1)."""
    counts = {"claude_code": 0, "copilot": 0, "skipped": 0}
    for resource_spans in payload.get("resourceSpans", []):
        attrs = resource_attrs_dict(resource_spans.get("resource", {}))
        vendor = detect_vendor(attrs)
        spans = [
            span
            for scope_spans in resource_spans.get("scopeSpans", [])
            for span in scope_spans.get("spans", [])
        ]
        if vendor == "copilot":
            copilot.handle_traces(attrs, spans, owner)
            counts["copilot"] += len(spans)
            _record_accepted(owner, "copilot")
        else:
            _log_undetected_vendor(attrs)
            _record_skipped(owner, attrs, len(spans))
            counts["skipped"] += len(spans)
    return counts
