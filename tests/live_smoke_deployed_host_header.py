"""Live smoke test against the REAL deployed service — the actual thing
that broke (commit e73d299: production requests hard-rejected with 421
"Invalid Host header"). test_transport_security.py regression-tests the
fixed code path in-process; this file instead hits the real deployed
URL the way the real chat UI's browser does: a genuine cross-origin POST
to https://ctxwindow.uk/mcp, with an Origin
header matching the deployed chat UI
(https://sre-agent.sohaibsohail.workers.dev) and — critically — a real
Host header that naturally follows from hitting the real URL over the
network, not a TestClient's synthetic one. A future regression in the
Cloudflare Worker proxy, Cloud Run's actual hostname, or the
MCP_ALLOWED_HOSTS env var deployed there would show up here as a live
421, the same way this bug was originally found, instead of only via a
user's screenshot.

Requires a real, valid bearer token for the deployed server — either
the owner token (MCP_AUTH_TOKEN, whatever was set on the Cloud Run
deployment) or a per-user token minted via /auth/login. A wrong/missing
token gets rejected by MultiTokenAuthMiddleware with 401 BEFORE the
request ever reaches the MCP transport's Host-header check — so this
smoke test needs a genuine token to mean anything; a 401 here proves
nothing about the bug this file exists to catch.

Not a pytest test (deliberately, matching sre-investigation-agent's
tests/browser_test_chat.py / tests/run_eval.py pattern for tests that
hit real infra and shouldn't run as part of the normal, hermetic
`uv run python -m pytest`). Run directly, with a real token:

    MCP_LIVE_SMOKE_TOKEN=<your real owner or per-user token> \
        uv run python -m tests.live_smoke_deployed_host_header

Skips (exit 0, not a failure) if MCP_LIVE_SMOKE_TOKEN isn't set — this
is meant to be run deliberately by whoever has a real token, not
silently in CI without one.
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEPLOYED_MCP_URL = "https://ctxwindow.uk/mcp"
CHAT_UI_ORIGIN = "https://sre-agent.sohaibsohail.workers.dev"


def _post_initialize(token):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "live-smoke-host-header", "version": "1.0.0"},
            },
        }
    ).encode()
    req = urllib.request.Request(
        DEPLOYED_MCP_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            # The real chat UI's cross-origin fetch always sends this —
            # CORSMiddleware on the deployed server allows it via
            # CHAT_UI_ORIGIN; the Host header itself is set by urllib
            # from DEPLOYED_MCP_URL, exactly like a real browser request,
            # not something we can or should fake here.
            "Origin": CHAT_UI_ORIGIN,
            # Cloudflare's bot protection in front of the Worker rejects
            # urllib's default "Python-urllib/x.y" user-agent outright
            # with its own 403 (error_code 1010), before this ever
            # reaches the origin server at all — a real browser's
            # user-agent avoids that unrelated failure mode.
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def main():
    token = os.environ.get("MCP_LIVE_SMOKE_TOKEN")
    if not token:
        print(
            "SKIPPED — MCP_LIVE_SMOKE_TOKEN not set. This smoke test needs a "
            "real bearer token for the deployed server (owner token or a "
            "per-user token from /auth/login) to mean anything; see this "
            "file's docstring."
        )
        return 0

    print(f"POSTing a real cross-origin initialize request to {DEPLOYED_MCP_URL} ...")
    status, text = _post_initialize(token)
    print(f"HTTP {status}")

    if status == 421:
        print("\n❌ FAIL — 421 Invalid Host header. This is exactly the bug "
              "fixed in commit e73d299 (or a regression of it): the deployed "
              "server's Host-header allowlist (MCP_ALLOWED_HOSTS) no longer "
              "covers the real hostname this request arrived with.")
        print(text)
        return 1

    if status == 401:
        print("\n⚠️  401 unauthorized — MCP_LIVE_SMOKE_TOKEN was rejected "
              "before ever reaching the Host-header check, so this run "
              "proves nothing about the 421 bug. Supply a real, valid "
              "token and re-run.")
        return 1

    if status >= 400:
        print(f"\n❌ FAIL — unexpected error status {status} (not the 421 "
              "this test targets, but not a clean success either).")
        print(text)
        return 1

    print("\n✅ PASS — real deployed cross-origin request succeeded, not 421.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
