from pathlib import Path

import config as config_module
from config import config


def test_the_suite_never_uses_the_real_config_json():
    real = Path(config_module.__file__).parent / "config.json"
    assert config._path.resolve() != real.resolve()


def test_saving_during_tests_leaves_the_real_config_json_alone(monkeypatch):
    real = Path(config_module.__file__).parent / "config.json"
    before = real.read_bytes() if real.exists() else None
    monkeypatch.setattr(config, "_data", {"only": "test values"})
    config.save()
    assert (real.read_bytes() if real.exists() else None) == before
