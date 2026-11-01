"""
/api/share/*: the guest side of a share link. No session: these are in auth.OPEN_PATHS and the
token in the JSON body is the whole credential (never in a URL, which reaches logs and Referer).
Every refusal of a token is the same 404, so a guess learns nothing.
"""
from aiohttp import web

from editor import editor_api as api
from editor import share_links as sl


def _link(body):
    link = sl.find(body.get("token"))
    if link is None:
        raise api._Fail(404, sl.GONE)
    return link


@api.map_errors
async def view(request):
    return api._no_store(api._ok(sl.view(_link(await api._body(request)))))


@api.map_errors
async def add_note(request):
    body = await api._body(request)
    link = _link(body)
    cid = api._client_id(body)
    if cid is None:
        raise api._Fail(400, "client_id is required")
    try:
        n = sl.add_note(link, body.get("name"), body.get("kind"), body.get("body"),
                        api._seconds(body.get("start_s")), api._seconds(body.get("end_s")), cid)
    except OverflowError as e:
        raise api._Fail(429, str(e))
    return api._ok({k: n[k] for k in ("id", "name", "kind", "start_s", "end_s", "body", "created_at")}, 201)


def register_routes(app: web.Application) -> None:
    app.router.add_post("/api/share/view", view)
    app.router.add_post("/api/share/notes", add_note)
