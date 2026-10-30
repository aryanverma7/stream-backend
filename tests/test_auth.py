import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json

import pytest
from aioresponses import aioresponses
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from core import auth
from core import db
from core.config import config


@pytest.fixture(autouse=True)
def clean_auth_state(tmp_path, monkeypatch):
    # session_for re-derives the role from config, so sessions need live accounts
    monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan", "DualBladeX"])
    monkeypatch.setitem(config._data, "editor_github_username", "CutGuy")
    db.use_path_for_tests(tmp_path / "auth.db")
    auth._pending_logins.clear()
    yield
    auth._pending_logins.clear()


def _const(resp):
    async def handler(request):
        return resp
    return handler


async def passthrough(request):
    return web.Response(text="handler ran")


def request_for(path: str, cookies: dict | None = None):
    headers = {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return make_mocked_request("GET", path, headers=headers)


class TestSafeNextPath:
    def test_keeps_a_local_path(self):
        assert auth._safe_next_path("/admin") == "/admin"

    def test_keeps_a_local_path_with_a_query_string(self):
        assert auth._safe_next_path("/admin?tab=points") == "/admin?tab=points"

    def test_rejects_an_absolute_url(self):
        assert auth._safe_next_path("https://evil.example") == auth.DEFAULT_POST_LOGIN_PATH

    def test_rejects_a_protocol_relative_url(self):
        # "//evil.example" is a valid Location header meaning https://evil.example
        assert auth._safe_next_path("//evil.example") == auth.DEFAULT_POST_LOGIN_PATH

    def test_rejects_a_backslash_that_browsers_read_as_a_slash(self):
        # Browsers normalise a backslash to "/" in a Location header: "/\evil.example" means //evil.example.
        for bad in ("/\\evil.example", "\\\\evil.example", "\\/evil.example"):
            assert auth._safe_next_path(bad) == auth.DEFAULT_POST_LOGIN_PATH, bad

    def test_rejects_control_characters_and_whitespace(self):
        # Browsers strip tab/newline from a Location header: "/\t/evil.example" becomes //evil.example.
        from urllib.parse import unquote
        for bad in (unquote("/%09/evil.example"), unquote("/%0a/evil.example"), "/\t/evil.example",
                    "/\r/evil", "/ /evil", "/admin\x00"):
            assert auth._safe_next_path(bad) == auth.DEFAULT_POST_LOGIN_PATH, repr(bad)

    def test_rejects_a_bare_relative_path(self):
        assert auth._safe_next_path("admin") == auth.DEFAULT_POST_LOGIN_PATH


class TestLogin:
    @pytest.mark.asyncio
    async def test_issues_a_state_and_remembers_where_to_return(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        with pytest.raises(web.HTTPFound) as caught:
            await auth.handle_login(request_for("/auth/login?next=/admin"))

        location = caught.value.location
        assert location.startswith(auth.GITHUB_AUTHORIZE_URL)
        assert "state=" in location
        assert len(auth._pending_logins) == 1
        assert list(auth._pending_logins.values()) == ["/admin"]

    @pytest.mark.asyncio
    async def test_defaults_to_the_dashboard_when_no_next_is_given(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        with pytest.raises(web.HTTPFound):
            await auth.handle_login(request_for("/auth/login"))

        assert list(auth._pending_logins.values()) == ["/admin"]

    @pytest.mark.asyncio
    async def test_will_not_carry_an_offsite_next_across_the_round_trip(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        with pytest.raises(web.HTTPFound):
            await auth.handle_login(request_for("/auth/login?next=https://evil.example"))

        assert list(auth._pending_logins.values()) == ["/admin"]

    @pytest.mark.asyncio
    async def test_pending_logins_do_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        for _ in range(auth._MAX_PENDING_LOGINS + 5):
            with pytest.raises(web.HTTPFound):
                await auth.handle_login(request_for("/auth/login?next=/admin"))

        assert len(auth._pending_logins) == auth._MAX_PENDING_LOGINS


class TestCallback:
    @pytest.mark.asyncio
    async def test_rejects_a_callback_with_no_state_at_all(self):
        response = await auth.handle_callback(request_for("/auth/callback?code=xyz"))
        assert response.status == 400

    @pytest.mark.asyncio
    async def test_rejects_a_state_we_never_issued(self):
        auth._pending_logins["ours"] = "/admin"
        response = await auth.handle_callback(request_for("/auth/callback?code=xyz&state=theirs"))
        assert response.status == 400
        # The genuine pending login must survive someone else's bad callback
        assert auth._pending_logins == {"ours": "/admin"}

    @pytest.mark.asyncio
    async def test_rejects_a_missing_code(self):
        response = await auth.handle_callback(request_for("/auth/callback"))
        assert response.status == 400


class TestMiddleware:
    @pytest.mark.asyncio
    async def test_open_paths_skip_authentication_entirely(self):
        for path in ("/", "/health", "/ws/widgets", "/api/public/clips", "/_next/static/x.js",
                     "/stickers/king.png", "/privacy", "/terms"):
            response = await auth.auth_middleware(request_for(path), passthrough)
            assert response.text == "handler ran", path

    @pytest.mark.asyncio
    async def test_the_agent_endpoints_are_open_so_their_own_secret_check_can_run(self):
        for path in ("/api/ocr/credit-report", "/api/ocr/reset", "/api/ocr/heartbeat",
                     "/api/uploader/live", "/api/uploader/projects/1/complete"):
            response = await auth.auth_middleware(request_for(path), passthrough)
            assert response.text == "handler ran", path

    @pytest.mark.asyncio
    async def test_root_is_matched_exactly_and_does_not_open_everything(self):
        response = await auth.auth_middleware(request_for("/api/status"), passthrough)
        assert response.status == 401

    @pytest.mark.asyncio
    async def test_a_session_less_dashboard_visit_redirects_into_the_login_flow(self):
        with pytest.raises(web.HTTPFound) as caught:
            await auth.auth_middleware(request_for("/admin"), passthrough)
        assert caught.value.location.startswith("/auth/login?")
        assert "next=" in caught.value.location

    @pytest.mark.asyncio
    async def test_an_api_call_gets_a_plain_401_rather_than_a_redirect(self):
        response = await auth.auth_middleware(request_for("/api/config"), passthrough)
        assert response.status == 401

    @pytest.mark.asyncio
    async def test_a_valid_session_cookie_reaches_the_handler(self):
        token = auth.create_session("aryan", "admin")
        request = request_for("/api/status", cookies={auth.SESSION_COOKIE_NAME: token})
        response = await auth.auth_middleware(request, passthrough)
        assert response.text == "handler ran"

    @pytest.mark.asyncio
    async def test_an_unknown_session_cookie_does_not(self):
        request = request_for("/api/status", cookies={auth.SESSION_COOKIE_NAME: "forged"})
        response = await auth.auth_middleware(request, passthrough)
        assert response.status == 401


class TestApiCallersAlwaysGetJson:
    """
    The dashboard calls .json() on every response, so a body that is not
    JSON surfaces in the browser as "JSON.parse: unexpected character at
    line 1 column 1" - a message that says only "not JSON" and is produced
    identically by an expired session, a route this backend does not have,
    and a Cloudflare error page while the backend restarts. Three
    different fixes behind one indistinguishable string, which has now
    cost a debugging session twice.

    Anything under /api/ therefore answers JSON even when it is refusing.
    """

    @pytest.mark.asyncio
    async def test_an_unauthenticated_api_call_gets_json(self, monkeypatch):
        monkeypatch.setattr(config, "_data", {})
        app = web.Application(middlewares=[auth.auth_middleware])
        app.router.add_get("/api/status", _const(web.json_response({"ok": True})))
        client = TestClient(TestServer(app))
        await client.start_server()

        resp = await client.get("/api/status")

        assert resp.status == 401
        assert resp.content_type == "application/json"
        body = await resp.json()
        assert "auth/login" in body["error"]
        await client.close()

    @pytest.mark.asyncio
    async def test_a_browser_navigation_still_gets_the_readable_text(self, monkeypatch):
        """
        Only /api/ changes. A person who typed a URL is not calling
        .json() on anything and is better served by a sentence.
        """
        monkeypatch.setattr(config, "_data", {})
        app = web.Application(middlewares=[auth.auth_middleware])
        app.router.add_get("/something", _const(web.Response(text="hi")))
        client = TestClient(TestServer(app))
        await client.start_server()

        resp = await client.get("/something")

        assert resp.status == 401
        assert resp.content_type == "text/plain"
        await client.close()

    @pytest.mark.asyncio
    async def test_admin_still_redirects_into_the_login_flow(self, monkeypatch):
        """The hidden dragon gesture has to land somewhere, not on an error."""
        monkeypatch.setattr(config, "_data", {})
        app = web.Application(middlewares=[auth.auth_middleware])
        app.router.add_get("/admin", _const(web.Response(text="admin")))
        client = TestClient(TestServer(app))
        await client.start_server()

        resp = await client.get("/admin", allow_redirects=False)

        assert resp.status == 302
        assert "/auth/login" in resp.headers["Location"]
        await client.close()


def _cookie(token):
    return request_for("/api/status", cookies={auth.SESSION_COOKIE_NAME: token})


class TestSessions:
    def test_a_session_survives_a_process_restart(self, tmp_path):
        token = auth.create_session("aryan", "admin")
        db.use_path_for_tests(tmp_path / "auth.db")  # drops the connection, reopens the file
        assert auth.session_for(_cookie(token)) == {"username": "aryan", "role": "admin"}

    def test_an_expired_session_is_rejected_and_removed(self):
        token = auth.create_session("aryan", "admin", now=0)
        assert auth.session_for(_cookie(token), now=auth.SESSION_MAX_AGE_SECONDS + 1) is None
        assert db.get().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    @pytest.mark.asyncio
    async def test_logout_deletes_the_session_and_clears_the_cookie(self):
        token = auth.create_session("aryan", "admin")
        resp = await auth.handle_logout(_cookie(token))
        assert auth.session_for(_cookie(token)) is None
        assert resp.status == 302 and resp.headers["Location"] == "/"
        cookie = resp.cookies[auth.SESSION_COOKIE_NAME]
        assert cookie["max-age"] == "0"

    @pytest.mark.asyncio
    async def test_the_login_cookie_is_secure_httponly_and_lax(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        auth._pending_logins["st"] = "/admin"
        with aioresponses() as mocked:
            mocked.post(auth.GITHUB_TOKEN_URL, payload={"access_token": "t"})
            mocked.get(auth.GITHUB_USER_URL, payload={"login": "Aryan"})
            with pytest.raises(web.HTTPFound) as caught:
                await auth.handle_callback(request_for("/auth/callback?code=c&state=st"))
        header = caught.value.cookies[auth.SESSION_COOKIE_NAME].OutputString()
        for part in ("Secure", "HttpOnly", "SameSite=Lax", "Max-Age=2592000"):
            assert part in header


class TestRoles:
    def test_role_for_username(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["DualBladeX"])
        monkeypatch.setitem(config._data, "editor_github_username", "CutGuy")
        assert auth.role_for_username("dualbladex") == "admin"
        assert auth.role_for_username("cutguy") == "editor"
        assert auth.role_for_username("someone") is None
        monkeypatch.setitem(config._data, "editor_github_username", "")
        assert auth.role_for_username("") is None

    @pytest.mark.asyncio
    async def test_open_paths_match_whole_segments(self):
        for path in ("/clipsadmin", "/healthz", "/api/publicity", "/_nextfoo"):
            resp = await auth.auth_middleware(request_for(path), passthrough)
            assert resp.status == 401, path
        for path in ("/clips/a.mp4", "/api/public/clips", "/_next/static/x.js", "/health"):
            resp = await auth.auth_middleware(request_for(path), passthrough)
            assert resp.text == "handler ran", path

    @pytest.mark.asyncio
    async def test_an_editor_session_cannot_reach_admin_apis(self):
        cookies = {auth.SESSION_COOKIE_NAME: auth.create_session("CutGuy", "editor")}
        for path in ("/api/config", "/api/config/schema", "/api/points/x", "/api/logs", "/api/status"):
            resp = await auth.auth_middleware(request_for(path, cookies), passthrough)
            assert resp.status == 403, path
            assert json.loads(resp.text) == {"error": "This account can only use the editor pages."}

    @pytest.mark.asyncio
    async def test_an_editor_visiting_admin_is_sent_to_the_editor_pages(self):
        cookies = {auth.SESSION_COOKIE_NAME: auth.create_session("CutGuy", "editor")}
        with pytest.raises(web.HTTPFound) as exc:
            await auth.auth_middleware(request_for("/admin", cookies), passthrough)
        assert exc.value.location == "/editor"

    @pytest.mark.asyncio
    async def test_an_editor_session_reaches_editor_paths(self):
        cookies = {auth.SESSION_COOKIE_NAME: auth.create_session("CutGuy", "editor")}
        for path in ("/editor", "/api/editor/projects", "/auth/logout"):
            resp = await auth.auth_middleware(request_for(path, cookies), passthrough)
            assert resp.text == "handler ran", path

    @pytest.mark.asyncio
    async def test_an_admin_session_reaches_everything(self):
        cookies = {auth.SESSION_COOKIE_NAME: auth.create_session("DualBladeX", "admin")}
        for path in ("/api/config", "/api/editor/projects"):
            resp = await auth.auth_middleware(request_for(path, cookies), passthrough)
            assert resp.text == "handler ran", path


class TestCallbackRoles:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("login,role,location", [("CutGuy", "editor", "/editor"), ("Aryan", "admin", "/admin")])
    async def test_the_session_carries_the_role_of_the_account(self, monkeypatch, login, role, location):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        monkeypatch.setitem(config._data, "editor_github_username", "CutGuy")
        auth._pending_logins["st"] = "/admin"
        with aioresponses() as mocked:
            mocked.post(auth.GITHUB_TOKEN_URL, payload={"access_token": "t"})
            mocked.get(auth.GITHUB_USER_URL, payload={"login": login})
            with pytest.raises(web.HTTPFound) as caught:
                await auth.handle_callback(request_for("/auth/callback?code=c&state=st"))
        assert caught.value.location == location
        token = caught.value.cookies[auth.SESSION_COOKIE_NAME].value
        found = auth.session_for(request_for("/x", {auth.SESSION_COOKIE_NAME: token}))
        assert found["role"] == role


    @pytest.mark.asyncio
    @pytest.mark.parametrize("next_path,location", [
        ("/editor#/p/12", "/editor#/p/12"),
        ("/editor?x=1#/p/12/t/5", "/editor?x=1#/p/12/t/5"),
        ("/admin#x", "/editor"),
        ("/admin?y#/editor", "/editor"),
    ])
    async def test_an_editors_deep_link_survives_login_but_admin_does_not(self, monkeypatch, next_path, location):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        monkeypatch.setitem(config._data, "editor_github_username", "CutGuy")
        auth._pending_logins["st"] = next_path
        with aioresponses() as mocked:
            mocked.post(auth.GITHUB_TOKEN_URL, payload={"access_token": "t"})
            mocked.get(auth.GITHUB_USER_URL, payload={"login": "CutGuy"})
            with pytest.raises(web.HTTPFound) as caught:
                await auth.handle_callback(request_for("/auth/callback?code=c&state=st"))
        assert caught.value.location == location


class TestRevocation:
    def _login(self, name, role):
        return _cookie(auth.create_session(name, role))

    def test_an_editor_session_dies_once_the_editor_is_cleared(self, monkeypatch):
        monkeypatch.setitem(config._data, "editor_github_username", "CutGuy")
        req = self._login("CutGuy", "editor")
        assert auth.session_for(req)["role"] == "editor"
        monkeypatch.setitem(config._data, "editor_github_username", "")
        assert auth.session_for(req) is None
        assert db.get().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0

    def test_a_removed_admin_loses_their_session(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        req = self._login("aryan", "admin")
        assert auth.session_for(req)["role"] == "admin"
        monkeypatch.setitem(config._data, "authorized_github_usernames", [])
        assert auth.session_for(req) is None

    def test_a_changed_role_is_picked_up(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        monkeypatch.setitem(config._data, "editor_github_username", "")
        req = self._login("aryan", "editor")
        assert auth.session_for(req) == {"username": "aryan", "role": "admin"}


class TestDotSegments:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["/_next/../admin", "/clips/./x"])
    async def test_dot_segments_are_refused(self, path):
        resp = await auth.auth_middleware(request_for(path), passthrough)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_normal_asset_path_is_still_open(self):
        resp = await auth.auth_middleware(request_for("/_next/static/a.js"), passthrough)
        assert resp.text == "handler ran"


class TestCallbackRefusals:
    async def _callback(self, login):
        auth._pending_logins["st"] = "/admin"
        with aioresponses() as mocked:
            mocked.post(auth.GITHUB_TOKEN_URL, payload={"access_token": "t"})
            mocked.get(auth.GITHUB_USER_URL, payload={"login": login})
            return await auth.handle_callback(request_for("/auth/callback?code=c&state=st"))

    @pytest.mark.asyncio
    async def test_a_stranger_gets_403(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", ["aryan"])
        monkeypatch.setitem(config._data, "editor_github_username", "")
        assert (await self._callback("stranger")).status == 403

    @pytest.mark.asyncio
    async def test_no_accounts_configured_gets_403(self, monkeypatch):
        monkeypatch.setitem(config._data, "authorized_github_usernames", [])
        monkeypatch.setitem(config._data, "editor_github_username", "")
        resp = await self._callback("anyone")
        assert resp.status == 403 and "not yet configured" in resp.text
