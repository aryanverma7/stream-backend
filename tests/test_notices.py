import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from core import db
from core.config import config
from editor import editor_bell
from editor import notices
from tests.test_editor_api import api, drive  # noqa: F401  (drive: fresh db + config)


def _outbox():
    return [dict(r) for r in db.get().execute("SELECT recipient_role, content, dedupe_key FROM dm_outbox")]


@pytest.mark.asyncio
async def test_streamer_posts_edits_deletes_and_editor_says_got_it():
    config._data.update({"discord_notifications_enabled": True})
    async with api() as a:
        assert (await a.editor("POST", "/api/editor/notices", {"text": "hi"}))[0] == 403
        for bad in ({}, {"text": "   "}, {"text": "x" * 2001}, {"text": 5}):
            assert (await a.admin("POST", "/api/editor/notices", bad))[0] == 400
        status, n = await a.admin("POST", "/api/editor/notices", {"text": " GTA 6 is out: clips this month "})
        assert status == 201 and n["text"] == "GTA 6 is out: clips this month" and n["seen_at"] is None
        status, n2 = await a.admin("POST", "/api/editor/notices", {"text": "<b>second</b>"})
        assert [x["id"] for x in (await a.editor("GET", "/api/editor/home"))[1]["notices"]] == [n2["id"], n["id"]]

        # one bell item and one DM per new notice; the edit re-notifies nobody
        assert (await a.editor("PATCH", f"/api/editor/notices/{n['id']}", {"text": "x"}))[0] == 403
        status, got = await a.admin("PATCH", f"/api/editor/notices/{n['id']}", {"text": "GTA 6: clips only"})
        assert status == 200 and got["text"] == "GTA 6: clips only"
        assert _outbox() == [
            {"recipient_role": "editor", "content": "New notice from aryan: GTA 6 is out: clips this month",
             "dedupe_key": f"notice:{n['id']}"},
            {"recipient_role": "editor", "content": "New notice from aryan: <b>second</b>",
             "dedupe_key": f"notice:{n2['id']}"}]
        bell = editor_bell.feed("cutguy")["items"]
        assert [i["kind"] for i in bell] == ["notice_posted"] * 2 and "second" in bell[0]["summary"]
        assert editor_bell.feed("aryan")["items"] == []

        assert (await a.admin("POST", f"/api/editor/notices/{n['id']}/seen"))[0] == 403
        status, got = await a.editor("POST", f"/api/editor/notices/{n['id']}/seen")
        first = got["seen_at"]
        assert status == 200 and first is not None
        assert (await a.editor("POST", f"/api/editor/notices/{n['id']}/seen"))[1]["seen_at"] == first
        assert (await a.editor("POST", "/api/editor/notices/999/seen"))[0] == 404

        assert (await a.editor("DELETE", f"/api/editor/notices/{n['id']}"))[0] == 403
        assert (await a.admin("DELETE", f"/api/editor/notices/{n['id']}"))[0] == 200
        assert (await a.admin("DELETE", f"/api/editor/notices/{n['id']}"))[0] == 404
        assert [x["id"] for x in (await a.editor("GET", "/api/editor/notices"))[1]["notices"]] == [n2["id"]]


def test_limit_is_2000_characters():
    assert notices.create("x" * 2000, "aryan")["text"] == "x" * 2000
