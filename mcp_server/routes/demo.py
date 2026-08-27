"""Demo-only static asset routes: serves the staged-reveal script and the
terminal mockup page used by scripts/demo_capture.py's recording (see
demo_static/demo_reveal.js and demo_static/demo_terminal.html).

Registered unconditionally (route registration itself is cheap and has
no per-user data), but CTXWINDOW_DEMO_MODE gates whether anything links
to these paths in practice: with the env var unset, as in every real
deployment, nothing on the dashboard references demo_reveal.js and
nothing sends a real user to demo_terminal.html, so both are dead code
in practice rather than a live surface. Not in
MultiTokenAuthMiddleware's protected_prefixes, same reasoning as /docs
and /m: static assets with no per-user data behind them."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse

from mcp_server.app import server

_DEMO_STATIC_DIR = Path(__file__).resolve().parent.parent / "demo_static"


@server.custom_route("/demo-static/demo_reveal.js", methods=["GET"])
async def demo_reveal_script(request: Request):
    return FileResponse(_DEMO_STATIC_DIR / "demo_reveal.js", media_type="text/javascript")


@server.custom_route("/demo-static/demo_terminal.html", methods=["GET"])
async def demo_terminal_page(request: Request):
    # Standalone page, not wired into any real route the way the
    # dashboard is: it renders a scripted terminal mockup only, no
    # session data, so it needs no auth gate beyond "you know the path".
    return FileResponse(_DEMO_STATIC_DIR / "demo_terminal.html", media_type="text/html")
