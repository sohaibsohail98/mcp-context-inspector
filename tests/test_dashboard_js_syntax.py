"""Regression test for a real bug found in review: the dashboard's
embedded <script> block lives inside a Python string in
mcp_server/server.py, so writing a literal `\n`/`\'` in the Python
source gets consumed by PYTHON's own escape processing before the JS
ever sees it. The browser then receives a raw newline/quote instead of
the two-character JS escape sequence, which is a JS SyntaxError severe
enough to kill the entire inline script (nothing on the dashboard page
works, not just the broken snippet). Static text-matching can't catch
this class of bug; only actually parsing the rendered output as JS can.
Skips if `node` isn't on PATH rather than failing. This is a
correctness net for local/CI environments that have it, not a hard
dependency for the rest of the suite.
"""

import re
import shutil
import subprocess

import pytest
from starlette.testclient import TestClient

from mcp_server import server as server_module

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")


def test_rendered_dashboard_script_is_valid_javascript(monkeypatch, tmp_path):
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    app = server_module.server.streamable_http_app()
    with TestClient(app) as client:
        resp = client.get("/auth/login")
    assert resp.status_code == 200

    scripts = re.findall(r"<script>(.*?)</script>", resp.text, re.S)
    assert scripts, "expected at least one inline <script> block in /auth/login's response"

    js_file = tmp_path / "extracted.js"
    js_file.write_text("\n".join(scripts))

    result = subprocess.run(
        ["node", "--check", str(js_file)], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, f"dashboard JS has a syntax error:\n{result.stderr}"
