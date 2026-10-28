import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config import Config, config


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


def test_save_replaces_the_file_atomically(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}')
    c = Config(path)
    c.set("a", 2)
    c.save()
    assert json.loads(path.read_text()) == {"a": 2}
    assert not (tmp_path / "config.json.tmp").exists()


def test_a_crash_mid_save_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}')
    c = Config(path)
    c.set("a", 2)
    def boom(*args, **kwargs):
        raise OSError("power cut")
    monkeypatch.setattr(json, "dump", boom)
    with pytest.raises(OSError):
        c.save()
    assert json.loads(path.read_text()) == {"a": 1}


def test_save_asks_the_drive_to_flush_when_full_fsync_exists(tmp_path, monkeypatch):
    from core import config as config_module
    calls = []
    monkeypatch.setattr(config_module.fcntl, "F_FULLFSYNC", 51, raising=False)
    monkeypatch.setattr(config_module.fcntl, "fcntl", lambda fd, cmd: calls.append(cmd))
    path = tmp_path / "config.json"
    path.write_text('{"a": 1}')
    c = Config(path)
    c.save()
    assert calls == [51]
