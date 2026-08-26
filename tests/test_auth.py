import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import auth
from config import config


@pytest.fixture(autouse=True)
def clean_auth_state():
    auth._valid_sessions.clear()
    auth._pending_logins.clear()
    yield
    auth._valid_sessions.clear()
    auth._pending_logins.clear()


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

    def test_rejects_a_bare_relative_path(self):
        assert auth._safe_next_path("admin") == auth.DEFAULT_POST_LOGIN_PATH


class TestLogin:
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

    async def test_defaults_to_the_dashboard_when_no_next_is_given(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        with pytest.raises(web.HTTPFound):
            await auth.handle_login(request_for("/auth/login"))

        assert list(auth._pending_logins.values()) == ["/admin"]

    async def test_will_not_carry_an_offsite_next_across_the_round_trip(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        with pytest.raises(web.HTTPFound):
            await auth.handle_login(request_for("/auth/login?next=https://evil.example"))

        assert list(auth._pending_logins.values()) == ["/admin"]

    async def test_pending_logins_do_not_grow_without_bound(self, monkeypatch):
        monkeypatch.setitem(config._data, "github_client_id", "abc123")
        monkeypatch.setitem(config._data, "public_base_url", "https://example.test")

        for _ in range(auth._MAX_PENDING_LOGINS + 5):
            with pytest.raises(web.HTTPFound):
                await auth.handle_login(request_for("/auth/login?next=/admin"))

        assert len(auth._pending_logins) == auth._MAX_PENDING_LOGINS


class TestCallback:
    async def test_rejects_a_callback_with_no_state_at_all(self):
        response = await auth.handle_callback(request_for("/auth/callback?code=xyz"))
        assert response.status == 400

    async def test_rejects_a_state_we_never_issued(self):
        auth._pending_logins["ours"] = "/admin"
        response = await auth.handle_callback(request_for("/auth/callback?code=xyz&state=theirs"))
        assert response.status == 400
        # The genuine pending login must survive someone else's bad callback
        assert auth._pending_logins == {"ours": "/admin"}

    async def test_rejects_a_missing_code(self):
        response = await auth.handle_callback(request_for("/auth/callback"))
        assert response.status == 400


class TestMiddleware:
    async def test_open_paths_skip_authentication_entirely(self):
        for path in ("/", "/health", "/ws/widgets", "/api/public/clips", "/_next/static/x.js"):
            response = await auth.auth_middleware(request_for(path), passthrough)
            assert response.text == "handler ran", path

    async def test_the_agent_endpoints_are_open_so_their_own_secret_check_can_run(self):
        for path in ("/api/ocr/credit-report", "/api/ocr/reset"):
            response = await auth.auth_middleware(request_for(path), passthrough)
            assert response.text == "handler ran", path

    async def test_root_is_matched_exactly_and_does_not_open_everything(self):
        response = await auth.auth_middleware(request_for("/api/status"), passthrough)
        assert response.status == 401

    async def test_a_session_less_dashboard_visit_redirects_into_the_login_flow(self):
        with pytest.raises(web.HTTPFound) as caught:
            await auth.auth_middleware(request_for("/admin"), passthrough)
        assert caught.value.location.startswith("/auth/login?")
        assert "next=" in caught.value.location

    async def test_an_api_call_gets_a_plain_401_rather_than_a_redirect(self):
        response = await auth.auth_middleware(request_for("/api/config"), passthrough)
        assert response.status == 401

    async def test_a_valid_session_cookie_reaches_the_handler(self):
        auth._valid_sessions.add("goodtoken")
        request = request_for("/api/status", cookies={auth.SESSION_COOKIE_NAME: "goodtoken"})
        response = await auth.auth_middleware(request, passthrough)
        assert response.text == "handler ran"

    async def test_an_unknown_session_cookie_does_not(self):
        request = request_for("/api/status", cookies={auth.SESSION_COOKIE_NAME: "forged"})
        response = await auth.auth_middleware(request, passthrough)
        assert response.status == 401
