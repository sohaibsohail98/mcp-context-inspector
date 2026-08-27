"""Demo-only static asset route: serves the staged-reveal script used by
scripts/demo_capture.py's recording (see demo_static/demo_reveal.js).

Registered unconditionally (route registration itself is cheap and has
no per-user data), but CTXWINDOW_DEMO_MODE gates whether auth.py ever
links to it: with the env var unset, as in every real deployment,
nothing on the dashboard references this path, so it is dead code in
practice rather than a live surface. Not in MultiTokenAuthMiddleware's
protected_prefixes, same reasoning as /docs and /m: a static asset with
no per-user data behind it."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse

from mcp_server.app import server

_DEMO_STATIC_DIR = Path(__file__).resolve().parent.parent / "demo_static"


@server.custom_route("/demo-static/demo_reveal.js", methods=["GET"])
async def demo_reveal_script(request: Request):
    return FileResponse(_DEMO_STATIC_DIR / "demo_reveal.js", media_type="text/javascript")
