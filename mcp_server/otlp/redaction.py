"""Redaction pass for raw OTEL body content captured under
OTEL_LOG_RAW_API_BODIES=1 (see mcp_server/otlp/claude_code.py's module
docstring). That opt-in is real, disclosed, and intentional. This
module does not change what gets captured, only strips a small set of
obviously-sensitive substrings (PII injected into system-reminder blocks,
etc.) before the raw text is stored as a context_block's `content`.

Design tradeoff: this list is deliberately SMALL and conservative, not a
fragile catch-all PII scrubber. Over-redacting real, useful debug content
(e.g. eating a legitimate code snippet with an `@` symbol, or a file path
in example code that isn't a real user path) is a real cost to the
Context Explorer's usefulness. Every pattern here is biased toward
precision over recall: it's fine to miss an edge case, it's not fine to
mangle unrelated content. Do not add broad patterns (generic "looks like
an ID" or "looks like a secret" heuristics) without the same scrutiny.
"""

import re

# Matches a standard email address (local@domain.tld shape). Deliberately
# a common/reasonable pattern, not RFC 5322-exact. Good enough to catch
# the injected-PII case (e.g. a user email in a <system-reminder> block)
# without trying to be a fully correct email validator.
_EMAIL_PATTERN = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

# Matches home-directory-style absolute file paths (`/Users/<name>/...`
# or `/home/<name>/...`) that would reveal a real local username. Anchored
# on a word boundary before the leading slash and requires at least one
# `/segment` after the username so it doesn't fire on a bare `/Users/` or
# `/home/` substring inside unrelated text (e.g. a URL path segment).
# Stops at whitespace/quote/paren/angle-bracket so it grabs just the path,
# not trailing prose.
_HOME_PATH_PATTERN = re.compile(
    r"(?<![\w/])/(?:Users|home)/[^/\s\"'()<>]+(?:/[^\s\"'()<>]*)?"
)

_EMAIL_PLACEHOLDER = "[redacted-email]"
_HOME_PATH_PLACEHOLDER = "[redacted-path]"


def redact(text):
    """Applies all redaction patterns in sequence and returns the
    redacted string. `None`/empty input is returned as-is (no-op) rather
    than raising, since callers may pass through blocks with no real
    content."""
    if not text:
        return text
    text = _EMAIL_PATTERN.sub(_EMAIL_PLACEHOLDER, text)
    text = _HOME_PATH_PATTERN.sub(_HOME_PATH_PLACEHOLDER, text)
    return text
