"""/docs: a standalone, single-page documentation site (installation,
usage, auth model, architecture), styled after miniblue.io with sidebar
section nav, no separate pages and no search, since ctxwindow's docs
content doesn't need that yet. Real file at docs-site/index.html, no
build step, same "static file, not a Python string template" pattern as
routes/webapp.py's mobile site.

/docs is NOT in MultiTokenAuthMiddleware's protected_prefixes. It's a
static asset with no per-user data, same reasoning as /m."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse

from mcp_server.app import server

_DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "docs-site"


@server.custom_route("/docs", methods=["GET"])
async def docs_root(request: Request):
    """Bare /docs (no trailing slash) -> /docs/, same reasoning as
    routes/webapp.py's webapp_root. Keeps the URL space consistent
    regardless of which form a user typed or bookmarked."""
    return RedirectResponse(url="/docs/")


@server.custom_route("/docs/", methods=["GET"])
async def docs_index(request: Request):
    return FileResponse(_DOCS_DIR / "index.html", media_type="text/html")
