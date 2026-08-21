"""Static-file routes for the mobile session-history webapp — a small,
purpose-built, single-column UI kept physically separate from the
desktop dashboard's inline-HTML-in-Python approach in routes/auth.py.
Real files under webapp/ at the repo root (index.html/app.js/styles.css,
no build step, same zero-dependency philosophy as the desktop
dashboard's own JS), served here rather than templated as Python
strings.

Route path: /m — short (easy to type/bookmark on a phone), and
distinct from /auth/login (the desktop entry point) and /api/* (the
REST data layer this page's JS calls client-side). /m itself is NOT in
MultiTokenAuthMiddleware's protected_prefixes, since these are just
static assets containing no per-user data — auth happens client-side
in app.js, which is gated at fetch time by the same bearer-token check
as everything else under /api/*.

Importing this module registers its routes on the shared `server`
instance (decorator side effect), same pattern as routes/api.py and
routes/setup.py."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse

from mcp_server.app import server

_WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent / "webapp"

_STATIC_FILES = {
    "app.js": ("app.js", "text/javascript"),
    "styles.css": ("styles.css", "text/css"),
}


@server.custom_route("/m", methods=["GET"])
async def webapp_root(request: Request):
    """Bare /m (no trailing slash) -> /m/ so the relative asset URLs in
    index.html (/m/app.js, /m/styles.css) resolve correctly regardless
    of which form a user typed or bookmarked."""
    return RedirectResponse(url="/m/")


@server.custom_route("/m/", methods=["GET"])
async def webapp_index(request: Request):
    return FileResponse(_WEBAPP_DIR / "index.html", media_type="text/html")


@server.custom_route("/m/{filename}", methods=["GET"])
async def webapp_static(request: Request):
    filename = request.path_params["filename"]
    entry = _STATIC_FILES.get(filename)
    if entry is None:
        return RedirectResponse(url="/m/")
    disk_name, media_type = entry
    return FileResponse(_WEBAPP_DIR / disk_name, media_type=media_type)
