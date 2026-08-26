"""Static-file routes (/m) for the mobile session-history webapp. Real
files under webapp/ at the repo root (index.html/app.js/styles.css, no
build step), served here rather than templated as Python strings the
way routes/auth.py's desktop dashboard is.

/m is NOT in MultiTokenAuthMiddleware's protected_prefixes, since these
are just static assets containing no per-user data — auth happens
client-side in app.js, gated at fetch time by the same bearer-token
check as everything else under /api/*."""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse

from mcp_server.app import server

_WEBAPP_DIR = Path(__file__).resolve().parent.parent.parent / "webapp"

# Allowlist of servable filenames -> media type. An allowlist, not a
# path join against the request, so `/m/../..`-style traversal can't
# reach anything outside webapp/.
_STATIC_FILES = {
    "app.js": "text/javascript",
    "styles.css": "text/css",
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
    media_type = _STATIC_FILES.get(filename)
    if media_type is None:
        return RedirectResponse(url="/m/")
    return FileResponse(_WEBAPP_DIR / filename, media_type=media_type)
