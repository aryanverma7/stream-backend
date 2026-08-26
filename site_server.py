"""
Serves the built Next.js static export (dualbladex-site/out) directly from
wherever it actually lives on disk - a sibling project folder, NOT copied
into this project. Pointing directly at the real out/ folder means every
future `npm run build` is immediately live with zero extra steps, rather
than needing a manual re-copy after every rebuild.

Handles three things aiohttp's plain static file serving does NOT do
automatically, confirmed by testing against a real Next.js static export:
  1. Serving index.html for a bare "/" request (aiohttp's static handler
     requires an exact filename match, it doesn't auto-serve a default
     document the way a typical web server does).
  2. Serving admin.html for "/admin" specifically - Next.js's static export
     names this file "admin.html" at the top level, not "admin/index.html".
  3. Correct route registration ORDER - this needs to be registered AFTER
     the existing /health, /api/*, /auth/*, /ws/widgets routes, so those
     specific routes are matched first and only truly unmatched paths fall
     through to serving site content.
"""
from pathlib import Path

from aiohttp import web

from config import config
from logger import get_logger

log = get_logger("SiteServer")


def _get_out_dir() -> Path:
    configured = config.get("site_out_dir", "")
    if not configured:
        raise RuntimeError(
            "site_out_dir is not set in config.json - point it at the "
            "absolute path to the frontend project's out/ folder, e.g. "
            "/Users/aryan/Documents/Site/out"
        )
    return Path(configured)


async def serve_home(request: web.Request) -> web.Response:
    out_dir = _get_out_dir()
    return web.FileResponse(out_dir / "index.html")


async def serve_admin(request: web.Request) -> web.Response:
    out_dir = _get_out_dir()
    return web.FileResponse(out_dir / "admin.html")


async def serve_root_file(request: web.Request) -> web.Response:
    """
    Generic catch-all for any file sitting directly in the out/ root -
    favicon.ico, dragon-original.js, and anything else dropped into the
    frontend project's public/ folder in the future. Replaces the earlier
    favicon-only route, since this exact class of bug (a new public/ file
    getting silently 401'd because nothing explicitly serves or allows it)
    has now happened twice - once for favicon.ico, once for
    dragon-original.js. A generic route fixes the SERVING side for any
    future file; the corresponding auth.py open_paths entry still needs to
    be added explicitly per file, deliberately, as a safety allowlist
    rather than a blanket opt-out.
    """
    out_dir = _get_out_dir()
    filename = request.match_info["filename"]

    # Path traversal guard - aiohttp's {filename} pattern already only
    # matches a single path segment (no slashes), but this is cheap
    # insurance against any unexpected match.
    if "/" in filename or ".." in filename:
        raise web.HTTPNotFound()

    file_path = out_dir / filename
    if not file_path.is_file():
        raise web.HTTPNotFound()

    return web.FileResponse(file_path)


def register_site_routes(app: web.Application):
    out_dir = _get_out_dir()

    app.router.add_get("/", serve_home)
    app.router.add_get("/admin", serve_admin)

    # The _next folder holds Next.js's generated JS/CSS bundles, which the
    # HTML files reference via /_next/... URLs - a plain static mount
    # handles this correctly since these ARE exact-filename requests.
    app.router.add_static("/_next", out_dir / "_next", show_index=False)

    # Registered LAST among these - only truly unmatched single-segment
    # paths (favicon.ico, dragon-original.js, etc.) fall through to this.
    app.router.add_get("/{filename}", serve_root_file)

    log.info(f"Serving site from {out_dir}")
