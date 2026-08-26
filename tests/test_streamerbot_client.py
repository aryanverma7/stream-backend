import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import AsyncMock, patch

import streamerbot_client


@pytest.mark.asyncio
async def test_forward_chat_broadcasts_twitch_message():
    event = {
        "event": {"type": "ChatMessage"},
        "data": {
            "source": {"platform": "twitch"},
            "message": {"username": "someviewer", "message": "hey chat"},
        },
    }

    with patch("streamerbot_client.widget_hub") as mock_hub:
        mock_hub.broadcast = AsyncMock()
        await streamerbot_client.forward_chat_to_widgets(event)

        mock_hub.broadcast.assert_awaited_once_with(
            {
                "type": "chat_message",
                "platform": "twitch",
                "username": "someviewer",
                "message": "hey chat",
            },
            tag="chat",
        )


@pytest.mark.asyncio
async def test_forward_chat_ignores_non_chat_events():
    event = {"event": {"type": "Follow"}, "data": {}}

    with patch("streamerbot_client.widget_hub") as mock_hub:
        mock_hub.broadcast = AsyncMock()
        await streamerbot_client.forward_chat_to_widgets(event)

        mock_hub.broadcast.assert_not_awaited()
