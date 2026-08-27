"""Tests for mcp_server/otlp/redaction.py.

Note on scope: a billing-header pattern (`x-anthropic-billing-header: ...`)
was considered per the plan this module implements, but was intentionally
NOT added. Reading mcp_server/otlp/claude_code.py's pipeline
(_parse_body -> _walk_request_body -> _blocks_from_message) shows the only
inputs ever walked into a context_block's content are
body.get("system")/body.get("tools")/body.get("messages"). The
JSON-decoded Anthropic Messages API request/response body.
x-anthropic-billing-header is an HTTP request header Claude Code sends
alongside that body, never part of the JSON body itself, so it cannot
reach stored block content through this pipeline. No pattern/test for it
is included here.
"""

import json

from mcp_server.otlp import claude_code
from mcp_server.otlp.redaction import redact


def test_redact_email_address():
    text = "Contact me at sscontactenquiries@gmail.com for details."
    result = redact(text)
    assert "[redacted-email]" in result
    assert "sscontactenquiries@gmail.com" not in result


def test_redact_home_directory_path():
    text = "Working directory: /Users/sohaibsohail/Projects/mcp-context-inspector"
    result = redact(text)
    assert "[redacted-path]" in result
    assert "sohaibsohail" not in result


def test_redact_home_path_variant():
    text = "cwd is /home/janedoe/repo/src and that's it"
    result = redact(text)
    assert "[redacted-path]" in result
    assert "janedoe" not in result


def test_redact_does_not_touch_unrelated_sentence():
    text = "Service X is degraded, please check the dashboard for status."
    assert redact(text) == text


def test_redact_does_not_touch_unrelated_url():
    text = "See https://example.com/Users/docs/getting-started for more info."
    # Not a home-directory path (no leading-slash /Users/ segment at a
    # path root, it's embedded in a URL), so it should pass through untouched.
    result = redact(text)
    assert result == text


def test_redact_does_not_touch_decorator_syntax():
    text = "Use @property or @staticmethod to decorate the method."
    assert redact(text) == text


def test_redact_none_and_empty_do_not_raise():
    assert redact(None) is None
    assert redact("") == ""


def test_redact_combined_email_and_path():
    text = (
        "<system-reminder>\n"
        "user email: sscontactenquiries@gmail.com\n"
        "cwd: /Users/sohaibsohail/Projects/mcp-context-inspector\n"
        "</system-reminder>"
    )
    result = redact(text)
    assert "sscontactenquiries@gmail.com" not in result
    assert "sohaibsohail" not in result
    assert "[redacted-email]" in result
    assert "[redacted-path]" in result


def test_stored_content_is_redacted_but_sizing_reflects_original(isolated_sqlite_db):
    """The size numbers (char_count/token_estimate) must be computed from
    the ORIGINAL, pre-redaction text, while the stored `content` is
    redacted. This is the "size numbers != stored preview" invariant.
    This test would fail if someone accidentally redacted-then-sized
    (i.e. computed char_count/token_estimate off the already-redacted
    text) instead of sizing-then-redacting."""
    store = isolated_sqlite_db

    original_text = (
        "<system-reminder>\nuser email: sscontactenquiries@gmail.com\n</system-reminder>\n\n"
        "What's the deploy process?"
    )
    request_body = {
        "messages": [{"role": "user", "content": original_text}],
    }
    record = {
        "timeUnixNano": "1000000000",
        "attributes": [
            {"key": "event.name", "value": {"stringValue": "api_request_body"}},
            {"key": "session.id", "value": {"stringValue": "sess-redact-size"}},
            {"key": "body", "value": {"stringValue": json.dumps(request_body)}},
        ],
        "body": {"stringValue": "claude_code.api_request_body"},
    }

    claude_code.handle_logs({"service.name": "claude-code"}, [record], owner=None)

    timeline = store.get_context_timeline("sess-redact-size")
    # The email lives inside the <system-reminder>, which split_injected_context
    # peels into its own `injected` block; the `user` block is the real prompt.
    injected_blocks = [b for b in timeline if b["category"] == "injected"]
    user_blocks = [b for b in timeline if b["category"] == "user"]
    assert len(injected_blocks) == 1
    assert len(user_blocks) == 1
    injected = injected_blocks[0]

    # Stored content must be redacted.
    assert "sscontactenquiries@gmail.com" not in injected["content"]
    assert "[redacted-email]" in injected["content"]

    # The per-fragment char_count is the ORIGINAL fragment length
    # (pre-redaction), so it does not equal the shorter redacted preview.
    assert injected["char_count"] != len(injected["content"])

    # Split invariant: the two fragments' char_count / token_estimate sum
    # back to the ORIGINAL whole message's, not the redacted text's.
    from mcp_server.otlp.common import estimate_tokens

    assert injected["char_count"] + user_blocks[0]["char_count"] == len(original_text)
    assert (
        injected["token_estimate"] + user_blocks[0]["token_estimate"]
        == estimate_tokens(original_text)
    )
