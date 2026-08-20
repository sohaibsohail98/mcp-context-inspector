"""OTLP telemetry ingestion — receives Claude Code's and GitHub
Copilot's native OpenTelemetry export (JSON protocol, `http/json`) and
maps it onto the same session/turn/tool_call/context_block schema
metrics/store.py already uses for our own Bedrock agent's
record_session(). See docs/internal/OTLP_INTEGRATION_PLAN.md for the full
research this is built against.

Dispatch is by OTLP resource attributes (`service.name` and friends) —
the exact attribute value each vendor stamps is asserted here per the
plan's docs research, but was NOT verified against a real captured
payload before this was written (see the plan's "Unverified" note).
If real payloads don't match, extend _detect_vendor's checks rather
than assuming the receiver route itself is wrong.
"""

from mcp_server.otlp import claude_code, copilot
from mcp_server.otlp.common import resource_attrs_dict

# Candidate service.name values for each vendor. Claude Code's telemetry
# docs don't spell out the exact resource attribute value anywhere in
# the pages this plan was researched against; "claude-code" is the
# reasonable default given every other Claude Code env var/metric is
# namespaced `claude_code.*` / `claude-code`. Verify and extend this set
# once a real payload has been captured locally (see build_order step 1
# in the plan).
_CLAUDE_CODE_SERVICE_NAMES = {"claude-code", "claude_code"}
_COPILOT_SERVICE_NAMES = {"github-copilot", "copilot", "github.copilot"}


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
        elif vendor == "copilot":
            copilot.handle_logs(attrs, log_records, owner)
            counts["copilot"] += len(log_records)
        else:
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
        elif vendor == "copilot":
            copilot.handle_metrics(attrs, metrics, owner)
            counts["copilot"] += len(metrics)
        else:
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
        else:
            counts["skipped"] += len(spans)
    return counts
