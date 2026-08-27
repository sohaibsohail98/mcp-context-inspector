"""Thin HTTP client for the live black-box API tests.

Deliberately stdlib-only (urllib), not httpx/requests: this subfolder's
whole point is to exercise a real deployed instance over the network, so
it has no dependency-injection or ASGI TestClient to piggyback on, and
adding an HTTP library dependency for what's a handful of JSON POST/GET
calls isn't worth it; see README.md for why this is a separate suite
from tests/ (which does use starlette's in-process TestClient)."""

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

# urllib's default User-Agent ("Python-urllib/3.x") is blocked outright by
# this deployment's Cloudflare-fronted WAF/bot-fight-mode (confirmed live,
# 2026-08-25: same request via curl succeeds, via bare urllib returns 403
# "error code: 1010" every time, not a rate limit; see the investigation
# report). A normal-looking UA avoids that entirely; this is also relevant
# to why real telemetry might not arrive if Claude Code's own OTel exporter
# has an equally unusual default UA.
_USER_AGENT = "mcp-context-inspector-api-tests/1.0"


@dataclass
class ApiResponse:
    status: int
    body: dict | list | None
    raw: bytes
    headers: dict


class ApiTestClient:
    """base_url/token come from env vars (API_TEST_BASE_URL, API_TEST_TOKEN)
    rather than constructor defaults, since these tests only make sense against
    a real deployment the caller has credentials for, never a guessed
    default, so a missing env var should fail loudly (see conftest.py)."""

    def __init__(self, base_url, token):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _request(self, method, path, body=None, headers=None, auth=True):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", _USER_AGENT)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return ApiResponse(resp.status, _try_json(raw), raw, dict(resp.headers))
        except urllib.error.HTTPError as e:
            raw = e.read()
            return ApiResponse(e.code, _try_json(raw), raw, dict(e.headers))

    def get(self, path, auth=True):
        return self._request("GET", path, auth=auth)

    def post(self, path, body, auth=True):
        return self._request("POST", path, body=body, auth=auth)

    def post_raw(self, path, raw_body, content_type="application/octet-stream", auth=True):
        """Like post(), but sends raw_body (bytes) unmodified instead of
        JSON-encoding it. Needed to test the server's malformed-JSON
        handling itself, where post()'s automatic json.dumps() would
        just re-encode a bad string into valid JSON."""
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url, data=raw_body, method="POST")
        req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", _USER_AGENT)
        if auth:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return ApiResponse(resp.status, _try_json(raw), raw, dict(resp.headers))
        except urllib.error.HTTPError as e:
            raw = e.read()
            return ApiResponse(e.code, _try_json(raw), raw, dict(e.headers))

    # --- typed convenience wrappers for the endpoints under test ---

    def health(self):
        return self.get("/health", auth=False)

    def sessions(self):
        return self.get("/api/sessions")

    def session_detail(self, session_id):
        return self.get(f"/api/sessions/{session_id}")

    def otlp_logs(self, resource_logs):
        return self.post("/otlp/v1/logs", {"resourceLogs": resource_logs})

    def otlp_metrics(self, resource_metrics):
        return self.post("/otlp/v1/metrics", {"resourceMetrics": resource_metrics})

    def otlp_traces(self, resource_spans):
        return self.post("/otlp/v1/traces", {"resourceSpans": resource_spans})

    def auth_verify(self, code):
        return self.post("/auth/verify", {"code": code}, auth=False)


def client_from_env():
    base_url = os.environ.get("API_TEST_BASE_URL")
    token = os.environ.get("API_TEST_TOKEN")
    if not base_url or not token:
        return None
    return ApiTestClient(base_url, token)


def _try_json(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None
