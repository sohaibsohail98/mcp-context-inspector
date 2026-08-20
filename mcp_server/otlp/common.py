"""Shared helpers for the per-vendor OTLP mappers — OTLP JSON wire-format
parsing (protobuf-JSON encoding, not the binary protobuf form) and the
chars-per-token fallback estimate. Both mappers import from here rather
than duplicating this parsing.
"""

from mci_common.config import CHARS_PER_TOKEN_ESTIMATE

# The five context_blocks categories the dashboard's CATEGORY_COLORS map
# and mci_common/timeline.py both already expect (see the recon note in
# docs/internal/OTLP_INTEGRATION_PLAN.md) — every mapper must emit one of these,
# never an invented category string, or the block silently fails to
# render/color in the existing dashboard.
CATEGORY_SYSTEM = "system"
CATEGORY_TOOLS = "tools"
CATEGORY_USER = "user"
CATEGORY_REASONING = "reasoning"
CATEGORY_TOOL_CALL = "tool_call"
CATEGORY_TOOL_RESULT = "tool_result"
CATEGORY_ANSWER = "answer"


def estimate_tokens(text):
    """Character-count fallback for content with no exact token count
    attached (see CHARS_PER_TOKEN_ESTIMATE's docstring in
    mci_common/config.py) — only use this when a real `usage` block
    isn't available for the content being sized."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN_ESTIMATE)


# A real AnyValue payload nests at most a couple of levels deep
# (attribute -> kvlistValue -> nested attribute). A cap well above any
# real payload but far below Python's default recursion limit stops a
# maliciously/accidentally deeply-nested arrayValue/kvlistValue from
# forcing a RecursionError on every request that touches it — found in
# review.
_MAX_OTLP_VALUE_DEPTH = 20


def _otlp_value(value_obj, _depth=0):
    """An OTLP JSON `AnyValue` is `{"stringValue": ...}` or
    `{"intValue": ...}` or `{"boolValue": ...}` or `{"doubleValue": ...}`
    etc — exactly one key present. Returns the unwrapped Python value.
    Unrecognized/empty AnyValue objects (e.g. `{}"` for a genuinely null
    attribute) return None rather than raising, since a single malformed
    attribute shouldn't sink an entire batch."""
    if not value_obj or _depth >= _MAX_OTLP_VALUE_DEPTH:
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value_obj:
            v = value_obj[key]
            return int(v) if key == "intValue" and isinstance(v, str) else v
    if "arrayValue" in value_obj:
        return [
            _otlp_value(v, _depth + 1) for v in value_obj["arrayValue"].get("values", [])
        ]
    if "kvlistValue" in value_obj:
        return attrs_list_to_dict(value_obj["kvlistValue"].get("values", []), _depth + 1)
    return None


def attrs_list_to_dict(attr_list, _depth=0):
    """OTLP JSON represents attribute lists as
    `[{"key": "...", "value": {"stringValue": "..."}}, ...]` — every
    resource/log/span/metric-datapoint attribute list in the wire format
    uses this exact shape. Converts to a plain `{key: value}` dict."""
    out = {}
    if _depth >= _MAX_OTLP_VALUE_DEPTH:
        return out
    for attr in attr_list or []:
        key = attr.get("key")
        if key is not None:
            out[key] = _otlp_value(attr.get("value", {}), _depth + 1)
    return out


def resource_attrs_dict(resource_obj):
    """resource_obj: the `"resource"` object on a resourceLogs/
    resourceMetrics/resourceSpans entry — `{"attributes": [...]}`."""
    return attrs_list_to_dict(resource_obj.get("attributes", []))


def log_record_body_text(log_record):
    """A LogRecord's `body` is itself an AnyValue — usually a
    stringValue carrying JSON (the raw Messages API body, for Claude
    Code's api_request_body/api_response_body events) or plain text.
    Returns the unwrapped value as-is (str, dict, or None) — callers
    that expect JSON should json.loads() a str result themselves and
    handle a decode failure explicitly rather than this helper guessing."""
    return _otlp_value(log_record.get("body", {}))
