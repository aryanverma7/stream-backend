import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from core import auth
from core import db
from core import server
from core.config import config

EDITOR_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
              "img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' https://www.googleapis.com https://*.googleusercontent.com; "
              "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    out = tmp_path / "out"
    (out / "_next").mkdir(parents=True)
    (out / "editor").mkdir()
    (out / "editor" / "media-sw.js").write_text("//sw")
    (out / "editor" / "other.js").write_text("//x")
    for name in ("index.html", "editor.html", "admin.html"):
        (out / name).write_text("<!doctype html><p>hi</p>")
    monkeypatch.setattr(config, "_data", {"authorized_github_usernames": ["aryan"],
                                          "editor_github_username": "cutguy", "site_out_dir": str(out)})
    c = TestClient(TestServer(server.build_app()))
    await c.start_server()
    yield c
    await c.close()


def _cookie(user, role):
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={auth.create_session(user, role)}"}


@pytest.mark.asyncio
async def test_editor_and_admin_pages_carry_the_full_csp(client):
    for path, who in (("/editor", ("cutguy", "editor")), ("/admin", ("aryan", "admin")),
                      ("/editor.html", ("aryan", "admin")), ("/admin.html", ("aryan", "admin"))):
        resp = await client.get(path, headers=_cookie(*who), allow_redirects=False)
        assert resp.status == 200 and resp.content_type == "text/html", path
        assert resp.headers["Content-Security-Policy"] == EDITOR_CSP, path
        assert resp.headers["X-Content-Type-Options"] == "nosniff", path


@pytest.mark.asyncio
async def test_homepage_and_api_only_forbid_framing(client):
    resp = await client.get("/")
    assert resp.status == 200
    assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    resp = await client.get("/index.html", headers=_cookie("aryan", "admin"))
    assert resp.status == 200 and resp.content_type == "text/html"
    assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    resp = await client.get("/api/editor/me", headers=_cookie("cutguy", "editor"))
    assert resp.status == 200
    assert resp.headers["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    resp = await client.get("/api/editor/me")  # refused by auth: still headed
    assert resp.status == 401 and resp.headers["X-Content-Type-Options"] == "nosniff"


def test_page_csp_allows_googleusercontent():
    assert "https://*.googleusercontent.com" in server.PAGE_CSP


@pytest.mark.asyncio
async def test_media_sw_gets_service_worker_allowed(client):
    resp = await client.get("/editor/media-sw.js", headers=_cookie("cutguy", "editor"))
    assert resp.status == 200 and resp.headers["Service-Worker-Allowed"] == "/editor"


@pytest.mark.asyncio
async def test_other_editor_files_do_not(client):
    resp = await client.get("/editor/other.js", headers=_cookie("cutguy", "editor"))
    assert resp.status == 200 and "Service-Worker-Allowed" not in resp.headers
