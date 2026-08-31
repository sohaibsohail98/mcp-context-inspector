"""Site-root static assets: /robots.txt, /sitemap.xml, /favicon.ico,
/favicon.svg.

Real crawlers (GoogleBot, LinkedInBot, Bing) and every browser request
these at the domain root; without them the access log fills with 404s
and crawlers get no guidance. Plain static files under static/, served
the same "FileResponse, no Python templating" way as routes/docs.py.

None of these paths are in MultiTokenAuthMiddleware's protected_prefixes,
so they're reachable unauthenticated -- which is the point.
"""

from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse

from mcp_server.app import server

_STATIC_DIR = Path(__file__).resolve().parent.parent.parent / "static"

# Cache at the edge for a day; these change rarely and the Cloudflare
# worker passes Cache-Control through untouched.
_CACHE = {"Cache-Control": "public, max-age=86400"}


@server.custom_route("/robots.txt", methods=["GET"])
async def robots_txt(request: Request):
    return FileResponse(_STATIC_DIR / "robots.txt", media_type="text/plain", headers=_CACHE)


@server.custom_route("/sitemap.xml", methods=["GET"])
async def sitemap_xml(request: Request):
    return FileResponse(_STATIC_DIR / "sitemap.xml", media_type="application/xml", headers=_CACHE)


@server.custom_route("/favicon.ico", methods=["GET"])
async def favicon_ico(request: Request):
    return FileResponse(_STATIC_DIR / "favicon.ico", media_type="image/x-icon", headers=_CACHE)


@server.custom_route("/favicon.svg", methods=["GET"])
async def favicon_svg(request: Request):
    return FileResponse(_STATIC_DIR / "favicon.svg", media_type="image/svg+xml", headers=_CACHE)
