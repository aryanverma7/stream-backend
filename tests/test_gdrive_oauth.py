import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aioresponses import aioresponses, CallbackResult
from urllib.parse import parse_qs, urlparse

from core import db
from editor import gdrive
from editor import gdrive_oauth
from core.config import config

FILES = f"{gdrive.API}/files?fields=id"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    db.use_path_for_tests(tmp_path / "x.db")
    monkeypatch.setattr(config, "_data", {
        "gdrive_client_id": "cid", "gdrive_client_secret": "sec",
        "gdrive_redirect_uri": "https://hub.example/auth/gdrive/callback",
    })
    monkeypatch.setattr(config, "save", lambda: None)
    gdrive_oauth._pending_states.clear()
    gdrive.forget_token()


async def make_client():
    app = web.Application()
    app.router.add_get("/auth/gdrive/login", gdrive_oauth.gdrive_login)
    app.router.add_get("/auth/gdrive/callback", gdrive_oauth.gdrive_callback)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def folder_posts(m, names):
    """Register one folder-create response per name, ids f-<name>."""
    for n in names:
        m.post(FILES, payload={"id": f"f-{n}"})


@pytest.mark.asyncio
async def test_login_redirect_carries_offline_consent_and_scope():
    client = await make_client()
    resp = await client.get("/auth/gdrive/login", allow_redirects=False)
    assert resp.status == 302
    loc = resp.headers["Location"]
    assert loc.startswith(gdrive.AUTH_URL + "?")
    q = parse_qs(urlparse(loc).query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == ["https://hub.example/auth/gdrive/callback"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == [gdrive.SCOPE]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["state"][0] in gdrive_oauth._pending_states
    await client.close()


@pytest.mark.asyncio
async def test_login_without_client_id_is_400(monkeypatch):
    monkeypatch.setattr(config, "_data", {})
    client = await make_client()
    resp = await client.get("/auth/gdrive/login", allow_redirects=False)
    assert resp.status == 400
    assert "gdrive_client_id" in (await resp.json())["error"]
    await client.close()


@pytest.mark.asyncio
async def test_callback_rejects_unknown_state():
    client = await make_client()
    resp = await client.get("/auth/gdrive/callback?code=c&state=nope", allow_redirects=False)
    assert resp.status == 400
    await client.close()


@pytest.mark.asyncio
async def test_pending_states_are_capped():
    for _ in range(40):
        gdrive_oauth._remember_state(gdrive_oauth.secrets.token_urlsafe(8))
    assert len(gdrive_oauth._pending_states) == 32


@pytest.mark.asyncio
async def test_callback_saves_refresh_token_and_builds_layout():
    gdrive_oauth._pending_states["s1"] = None
    bodies = []

    def tok(url, **kw):
        bodies.append(kw["data"])
        return CallbackResult(payload={"access_token": "a", "refresh_token": "r1", "expires_in": 3600})

    client = await make_client()
    with aioresponses(passthrough=[str(client.make_url('/'))]) as m:
        m.post(gdrive.TOKEN_URL, callback=tok)
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})  # layout's own access token
        folder_posts(m, ["root", "projects", "brand", "backups"])
        resp = await client.get("/auth/gdrive/callback?code=c&state=s1", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/admin"
    assert bodies[0]["grant_type"] == "authorization_code"
    assert bodies[0]["code"] == "c" and bodies[0]["client_secret"] == "sec"
    assert config.get("gdrive_refresh_token") == "r1"
    assert [db.meta_get(k) for k in ("gdrive_root", "gdrive_projects", "gdrive_brand_kit", "gdrive_backups")] == \
        ["f-root", "f-projects", "f-brand", "f-backups"]
    await client.close()


@pytest.mark.asyncio
async def test_callback_without_refresh_token_is_400():
    gdrive_oauth._pending_states["s1"] = None
    client = await make_client()
    with aioresponses(passthrough=[str(client.make_url('/'))]) as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a"})
        resp = await client.get("/auth/gdrive/callback?code=c&state=s1", allow_redirects=False)
    assert resp.status == 400
    assert "consent" in (await resp.json())["error"]
    assert not config.get("gdrive_refresh_token")
    await client.close()


@pytest.mark.asyncio
async def test_callback_layout_failure_keeps_token_and_answers_502():
    gdrive_oauth._pending_states["s1"] = None
    client = await make_client()
    with aioresponses(passthrough=[str(client.make_url('/'))]) as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "refresh_token": "r1", "expires_in": 3600})
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        m.post(FILES, status=500, payload={"error": "boom"})
        resp = await client.get("/auth/gdrive/callback?code=c&state=s1", allow_redirects=False)
    assert resp.status == 502
    assert config.get("gdrive_refresh_token") == "r1"
    await client.close()


async def connected():
    config._data["gdrive_refresh_token"] = "r"
    return None


@pytest.mark.asyncio
async def test_ensure_layout_is_idempotent():
    await connected()
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        folder_posts(m, ["root", "projects", "brand", "backups"])
        first = await gdrive_oauth.ensure_layout()
    with aioresponses() as m:  # nothing registered: any request would raise
        second = await gdrive_oauth.ensure_layout()
    assert first == second and len(first) == 4


@pytest.mark.asyncio
async def test_ensure_layout_partial_failure_keeps_created_ids():
    await connected()
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        folder_posts(m, ["root", "projects"])
        m.post(FILES, status=500, payload={"error": "boom"})
        with pytest.raises(gdrive.DriveError):
            await gdrive_oauth.ensure_layout()
    assert db.meta_get("gdrive_root") == "f-root" and db.meta_get("gdrive_projects") == "f-projects"
    assert db.meta_get("gdrive_brand_kit") is None
    with aioresponses() as m:
        folder_posts(m, ["brand", "backups"])  # only the rest: root/projects must not be re-created
        ids = await gdrive_oauth.ensure_layout()
    assert ids["gdrive_root"] == "f-root" and ids["gdrive_backups"] == "f-backups"


@pytest.mark.asyncio
async def test_brand_kit_shared_once_when_editor_email_set():
    await connected()
    config._data["editor_google_email"] = "ed@example.com"
    perm = f"{gdrive.API}/files/f-brand/permissions?sendNotificationEmail=false"
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        folder_posts(m, ["root", "projects", "brand", "backups"])
        m.post(perm, payload={})
        await gdrive_oauth.ensure_layout()
    assert db.meta_get("gdrive_brand_kit_shared_with") == "ed@example.com"
    with aioresponses() as m:  # a second share would hit an unregistered URL
        await gdrive_oauth.ensure_layout()


ABOUT_USER = f"{gdrive.API}/about?fields=user%28emailAddress%29"


def perm_url(fid):
    return f"{gdrive.API}/files/{fid}/permissions?sendNotificationEmail=false"


@pytest.mark.asyncio
async def test_brand_kit_reshared_when_the_editor_email_changes():
    await connected()
    config._data["editor_google_email"] = "ed@example.com"
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        folder_posts(m, ["root", "projects", "brand", "backups"])
        m.post(perm_url("f-brand"), payload={})
        await gdrive_oauth.ensure_layout()
    assert db.meta_get("gdrive_brand_kit_shared_with") == "ed@example.com"
    config._data["editor_google_email"] = "new@example.com"
    shares = []
    with aioresponses() as m:
        m.post(perm_url("f-brand"), callback=lambda url, **kw: shares.append(kw["json"]) or CallbackResult(payload={}))
        await gdrive_oauth.ensure_layout()
    assert shares and shares[0]["emailAddress"] == "new@example.com"
    assert db.meta_get("gdrive_brand_kit_shared_with") == "new@example.com"


async def _callback_with_account(email):
    gdrive_oauth._pending_states["s1"] = None
    client = await make_client()
    with aioresponses(passthrough=[str(client.make_url('/'))]) as m:
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "refresh_token": "r1", "expires_in": 3600})
        m.post(gdrive.TOKEN_URL, payload={"access_token": "a", "expires_in": 3600})
        m.get(ABOUT_USER, payload={"user": {"emailAddress": email}})
        folder_posts(m, ["root2", "projects2", "brand2", "backups2"])
        resp = await client.get("/auth/gdrive/callback?code=c&state=s1", allow_redirects=False)
    await client.close()
    return resp


@pytest.mark.asyncio
async def test_reconnecting_a_different_account_rebuilds_the_layout():
    for k in gdrive_oauth._LAYOUT:
        db.meta_set(k, "old-" + k)
    db.meta_set("gdrive_account", "old@example.com")
    db.meta_set("gdrive_brand_kit_shared_with", "ed@example.com")
    resp = await _callback_with_account("new@example.com")
    assert resp.status == 302
    assert db.meta_get("gdrive_root") == "f-root2" and db.meta_get("gdrive_backups") == "f-backups2"
    assert db.meta_get("gdrive_account") == "new@example.com"
    assert db.meta_get("gdrive_brand_kit_shared_with") is None


@pytest.mark.asyncio
async def test_reconnecting_the_same_account_keeps_the_layout():
    for k in gdrive_oauth._LAYOUT:
        db.meta_set(k, "old-" + k)
    db.meta_set("gdrive_account", "me@example.com")
    resp = await _callback_with_account("me@example.com")
    assert resp.status == 302 and db.meta_get("gdrive_root") == "old-gdrive_root"


@pytest.mark.asyncio
async def test_ensure_layout_waits_for_the_shared_folder_lock(monkeypatch):
    import asyncio
    from editor import editor_service
    assert editor_service._folder_lock is gdrive_oauth._folder_lock
    # A lock that waited is bound to this test's loop; use a fresh one so later tests are unaffected.
    monkeypatch.setattr(gdrive_oauth, "_folder_lock", asyncio.Lock())
    for k in gdrive_oauth._LAYOUT:
        db.meta_set(k, "x-" + k)
    async with gdrive_oauth._folder_lock:
        task = asyncio.create_task(gdrive_oauth.ensure_layout())
        await asyncio.sleep(0.01)
        assert not task.done()
    assert (await task)["gdrive_root"] == "x-gdrive_root"
