import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import config


def test_public_safe_only_returns_allowlisted_keys(monkeypatch):
    monkeypatch.setattr(config, "_data", {
        "http_host": "0.0.0.0",
        "http_port": 8765,
        "streamlabs_access_token": "super-secret-token",
        "github_client_secret": "another-secret",
        "social_links": {
            "twitch": "https://www.twitch.tv/dualbladex",
            "youtube": "https://www.youtube.com/@DualBladeX",
            "instagram": "https://www.instagram.com/dualbladex7/",
            "discord": "https://discord.gg/jqAqSfrqYY",
        },
    })

    result = config.public_safe()

    assert result == {
        "social_links": {
            "twitch": "https://www.twitch.tv/dualbladex",
            "youtube": "https://www.youtube.com/@DualBladeX",
            "instagram": "https://www.instagram.com/dualbladex7/",
            "discord": "https://discord.gg/jqAqSfrqYY",
        }
    }
    assert "super-secret-token" not in str(result)
    assert "another-secret" not in str(result)
