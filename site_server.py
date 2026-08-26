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

Real bug this cost us: the catch-all used to be `/{filename}`, which
aiohttp matches against a SINGLE path segment only. That was fine while
the export was flat, but Next 16's client-side router fetches its route
data from NESTED paths - navigating to /admin without a full page load
requests /admin/__next._tree.txt and /admin/__next.admin.__PAGE__.txt,
both of which the export really does contain (out/admin/) and neither of
which a single-segment route can ever match. The router got a 404 for its
route data and rendered "This page couldn't load"; pressing reload worked
because that is a real document request for /admin, which serve_admin
answers from admin.html without any route data being involved. The
catch-all now matches a full path and resolves it the way a normal static
web server would, so anything the export emits at any depth is served.
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


def _resolve_within(out_dir: Path, relative: str) -> "Path | None":
    """
    Maps a request path onto a real file inside out_dir, or None if there
    isn't one. Tries the three forms a static host is expected to
    understand, in the order a normal web server would:

      /admin/__next._tree.txt -> out/admin/__next._tree.txt   (exact file)
      /some-page              -> out/some-page.html           (Next's own
                                  extensionless page naming)
      /some-dir               -> out/some-dir/index.html      (directory
                                  default document)

    The containment check below is the security-relevant part: resolve()
    collapses any ".." before the comparison, so a crafted path can only
    ever land on a file that is genuinely inside out_dir.
    """
    root = out_dir.resolve()
    base = (out_dir / relative).resolve()
    if base != root and root not in base.parents:
        return None

    candidates = (base, base.with_name(base.name + ".html"), base / "index.html")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


async def serve_site_file(request: web.Request) -> web.Response:
    """
    Generic catch-all for everything in the export that isn't one of the
    explicit routes above - favicon.ico, dragon-original.js, the Next.js
    router's own route-data .txt files, and anything else dropped into the
    frontend project's public/ folder in the future. This replaced a
    favicon-only route first, then a single-segment one, because this exact
    class of bug (something in the export getting silently 404'd or 401'd
    because nothing explicitly serves or allows it) has now happened three
    times - favicon.ico, dragon-original.js, and the router's nested route
    data. A generic route fixes the SERVING side for anything the export
    emits; the corresponding auth.py open_paths entry still needs to be
    added explicitly per public file, deliberately, as a safety allowlist
    rather than a blanket opt-out.
    """
    out_dir = _get_out_dir()
    file_path = _resolve_within(out_dir, request.match_info["path"])
    if file_path is None:
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

    # Registered LAST among these - only truly unmatched paths fall
    # through to this. The pattern matches a full path including slashes,
    # not one segment, so nested export files (the Next.js router's own
    # /admin/__next.*.txt route data) are reachable.
    app.router.add_get("/{path:.*}", serve_site_file)

    log.info(f"Serving site from {out_dir}")
