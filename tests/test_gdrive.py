import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from aioresponses import aioresponses

import gdrive
from config import config

API = gdrive.API
TOKEN = {"access_token": "tok", "expires_in": 3600}


@pytest.fixture(autouse=True)
def connected(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "gdrive_client_id": "cid", "gdrive_client_secret": "sec", "gdrive_refresh_token": "rt",
    })
    monkeypatch.setattr(config, "save", lambda: None)
    gdrive.forget_token()
    yield
    gdrive.forget_token()


def calls(m, method, url_start):
    return [(k, v) for k, vs in m.requests.items() for v in vs
            if k[0] == method and str(k[1]).startswith(url_start)]


@pytest.mark.asyncio
async def test_refresh_caches_token_until_near_expiry():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN, repeat=True)
        assert await gdrive.access_token(now=1000.0) == "tok"
        assert await gdrive.access_token(now=1000.0 + 3500) == "tok"
        assert len(calls(m, "POST", gdrive.TOKEN_URL)) == 1
        await gdrive.access_token(now=1000.0 + 3550)  # inside the 60 s margin
        assert len(calls(m, "POST", gdrive.TOKEN_URL)) == 2


@pytest.mark.asyncio
async def test_rotated_refresh_token_is_saved(monkeypatch):
    saved = []
    monkeypatch.setattr(config, "save", lambda: saved.append(1))
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload={**TOKEN, "refresh_token": "new"})
        await gdrive.access_token(now=0.0)
    assert config._data["gdrive_refresh_token"] == "new"
    assert saved


@pytest.mark.asyncio
async def test_missing_refresh_token_raises_not_connected(monkeypatch):
    monkeypatch.setitem(config._data, "gdrive_refresh_token", "")
    with pytest.raises(gdrive.DriveNotConnected):
        await gdrive.access_token(now=0.0)


@pytest.mark.asyncio
async def test_refused_refresh_raises_not_connected():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, status=400, payload={"error": "invalid_grant"})
        with pytest.raises(gdrive.DriveNotConnected):
            await gdrive.access_token(now=0.0)


@pytest.mark.asyncio
async def test_delete_404_is_success():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.delete(f"{API}/files/abc", status=404)
        assert await gdrive.delete_file("abc") is None


@pytest.mark.asyncio
async def test_delete_500_raises_drive_error_with_status():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.delete(f"{API}/files/abc", status=500, payload={"error": {"message": "boom"}})
        with pytest.raises(gdrive.DriveError) as e:
            await gdrive.delete_file("abc")
    assert e.value.status == 500


@pytest.mark.asyncio
async def test_resumable_upload_returns_location_and_sends_origin():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.post(f"{gdrive.UPLOAD_API}?uploadType=resumable", status=200,
               headers={"Location": "https://upload.example/session1"})
        url = await gdrive.start_resumable_upload(
            "v1 - a.mp4", "par", 1234, "video/mp4", {"iteration_id": "7"}, "https://site.example")
        (_, req), = calls(m, "POST", gdrive.UPLOAD_API)
    assert url == "https://upload.example/session1"
    h = req.kwargs["headers"]
    assert h["Origin"] == "https://site.example"
    assert h["X-Upload-Content-Length"] == "1234"
    assert h["X-Upload-Content-Type"] == "video/mp4"
    assert req.kwargs["json"] == {"name": "v1 - a.mp4", "parents": ["par"], "appProperties": {"iteration_id": "7"}}


@pytest.mark.asyncio
async def test_401_from_drive_refreshes_once_and_retries():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN, repeat=True)
        m.delete(f"{API}/files/abc", status=401)
        m.delete(f"{API}/files/abc", status=204)
        await gdrive.delete_file("abc")
        assert len(calls(m, "POST", gdrive.TOKEN_URL)) == 2
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN, repeat=True)
        m.delete(f"{API}/files/abc", status=401, repeat=True)
        with pytest.raises(gdrive.DriveError) as e:
            await gdrive.delete_file("abc")
    assert e.value.status == 401


@pytest.mark.asyncio
async def test_list_folder_follows_next_page_token():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        pages = [
            {"files": [{"id": "1", "size": "10"}], "nextPageToken": "p2"},
            {"files": [{"id": "2"}]},
        ]
        m.get(re.compile(rf"^{re.escape(API)}/files\?.*"), payload=pages[0])
        m.get(re.compile(rf"^{re.escape(API)}/files\?.*"), payload=pages[1])
        out = await gdrive.list_folder("fold")
        gets = calls(m, "GET", f"{API}/files")
    assert [f["id"] for f in out] == ["1", "2"]
    assert out[0]["size"] == 10
    assert "pageToken=p2" in str(gets[1][0][1])


@pytest.mark.asyncio
async def test_about_handles_missing_limit():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.get(f"{API}/about?fields=storageQuota", payload={"storageQuota": {"usage": "5"}})
        assert await gdrive.about() == {"limit": None, "usage": 5}


@pytest.mark.asyncio
async def test_upload_small_file_puts_bytes_and_returns_id(tmp_path):
    f = tmp_path / "b.db"
    f.write_bytes(b"12345")
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.post(f"{gdrive.UPLOAD_API}?uploadType=resumable", headers={"Location": "https://up.example/s"})
        m.put("https://up.example/s", payload={"id": "fid"})
        assert await gdrive.upload_small_file(f, "b.db", "par") == "fid"
        (_, req), = calls(m, "PUT", "https://up.example/s")
    assert req.kwargs["data"] == b"12345"
    assert req.kwargs["headers"]["Content-Length"] == "5"


@pytest.mark.asyncio
async def test_html_502_from_token_endpoint_is_drive_error():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, status=502, body="<html>Bad Gateway</html>")
        with pytest.raises(gdrive.DriveError) as e:
            await gdrive.access_token(now=0.0)
    assert e.value.status == 502


@pytest.mark.asyncio
async def test_html_400_from_token_endpoint_is_not_connected():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, status=400, body="<html>Bad Request</html>")
        with pytest.raises(gdrive.DriveNotConnected):
            await gdrive.access_token(now=0.0)


@pytest.mark.asyncio
async def test_token_503_json_is_drive_error_not_not_connected():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, status=503, payload={"error": "backend_error"})
        with pytest.raises(gdrive.DriveError) as e:
            await gdrive.access_token(now=0.0)
    assert not isinstance(e.value, gdrive.DriveNotConnected)
    assert e.value.status == 503


@pytest.mark.asyncio
async def test_empty_200_from_token_endpoint_is_drive_error():
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, status=200, body="")
        with pytest.raises(gdrive.DriveError):
            await gdrive.access_token(now=0.0)


@pytest.mark.asyncio
async def test_network_error_on_drive_call_is_drive_error_status_0():
    import aiohttp
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.delete(f"{API}/files/abc", exception=aiohttp.ClientConnectionError())
        with pytest.raises(gdrive.DriveError) as e:
            await gdrive.delete_file("abc")
    assert e.value.status == 0


@pytest.mark.asyncio
async def test_small_file_put_uses_long_timeout_metadata_keeps_60s(tmp_path):
    f = tmp_path / "b.db"
    f.write_bytes(b"1")
    with aioresponses() as m:
        m.post(gdrive.TOKEN_URL, payload=TOKEN)
        m.post(f"{gdrive.UPLOAD_API}?uploadType=resumable", headers={"Location": "https://up.example/s"})
        m.put("https://up.example/s", payload={"id": "fid"})
        await gdrive.upload_small_file(f, "b.db", "par")
        (_, put), = calls(m, "PUT", "https://up.example/s")
        (_, post), = calls(m, "POST", gdrive.UPLOAD_API)
    t = put.kwargs["timeout"]
    assert (t.total, t.sock_connect, t.sock_read) == (None, 30, 120)
    assert post.kwargs["timeout"].total == 60
