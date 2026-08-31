"""Regression test for a real bug found in review: the dashboard's JS
used to live inside a Python f-string in routes/auth.py, so a literal
`\n`/`\'` in the Python source got consumed by PYTHON's own escape
processing before the JS ever saw it -- the browser then received a raw
newline/quote instead of the two-character JS escape, a SyntaxError
severe enough to kill the whole script. The JS is now a real static file
(dashboard/dashboard.js), which removes that specific hazard, but this
still parses the served output as JS to catch any regression: both the
served dashboard.js and every inline <script> block auth_login still
injects (currently just the window.__CFG__ config block).
Skips if `node` isn't on PATH rather than failing.
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
        dashboard_js = client.get("/auth/static/dashboard.js")
    assert dashboard_js.status_code == 200
    assert "text/javascript" in dashboard_js.headers["content-type"]

    inline_scripts = re.findall(r"<script>(.*?)</script>", resp.text, re.S)
    assert inline_scripts, "expected at least one inline <script> block (the window.__CFG__ config) in /auth/login"

    # The inline config block references window.__CFG__ which the external
    # file reads; concatenate config-first so `node --check` sees a
    # coherent program.
    js_file = tmp_path / "extracted.js"
    js_file.write_text("\n".join(inline_scripts) + "\n" + dashboard_js.text)

    result = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, f"dashboard JS has a syntax error:\n{result.stderr}"
