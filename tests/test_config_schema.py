import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config_schema import (FIELD_TYPES, FIELDS, SECRET_MASK, SECTIONS, masked, with_needed,
                           schema_for, validate_update)

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"


def test_needed_is_true_or_names_a_bool_setting():
    needed = {k: f["needed"] for k, f in FIELDS.items() if "needed" in f}
    assert needed["gdrive_refresh_token"] is True
    assert needed["discord_bot_token"] == "discord_notifications_enabled"
    for key, cond in needed.items():
        assert cond is True or FIELDS[cond]["type"] == "bool", key


def test_with_needed_adds_missing_needed_keys_empty_and_keeps_real_values():
    out = with_needed({"github_client_id": "abc"})
    assert out["github_client_id"] == "abc"
    assert out["gdrive_refresh_token"] == ""
    assert out["authorized_github_usernames"] == []
    assert "roulette_trigger_cost" not in out


def test_every_example_key_has_metadata():
    for key in json.loads(EXAMPLE.read_text()):
        if key.startswith("_"):
            continue
        f = FIELDS[key]
        assert f["label"] and f["description"]
        assert f["section"] in SECTIONS and f["section"] != "Other"
        assert f["type"] in FIELD_TYPES


def test_secrets_are_masked_and_unset_secrets_stay_empty():
    out = masked({"github_client_secret": "abc", "spotify_refresh_token": "", "http_port": 8765})
    assert out == {"github_client_secret": SECRET_MASK, "spotify_refresh_token": "", "http_port": 8765}


def test_an_unknown_key_that_looks_secret_is_masked():
    assert masked({"new_service_api_key": "zzz"})["new_service_api_key"] == SECRET_MASK


def test_schema_infers_unknown_keys_into_other():
    f = schema_for({"foo_list": ["a"], "foo_map": {"a": [1]}})["fields"]
    assert (f["foo_list"]["type"], f["foo_list"]["section"]) == ("list", "Other")
    assert (f["foo_map"]["type"], f["foo_map"]["section"]) == ("json", "Other")


def test_validate_number_bounds():
    data = {"_custom_fields": {"k": {"type": "number", "section": "Other"}}, "k": 20}
    assert validate_update({"discord_live_poll_seconds": 5}, data) == (
        "discord_live_poll_seconds", "Must be 15 seconds or more")


def test_validate_rejects_bad_new_key_and_private_keys():
    assert validate_update({"Bad-Key": 1}, {})[0] == "Bad-Key"
    assert validate_update({"_x": 1}, {})[0] == "_x"


def test_secret_looking_name_stays_masked_whatever_its_custom_type():
    data = {"foo_api_key": "raw", "_custom_fields": {"foo_api_key": {"type": "text", "section": "Other"}}}
    assert masked(data)["foo_api_key"] == SECRET_MASK


def test_break_default_minutes_is_in_break_screen_before_login_and_site():
    assert FIELDS["break_default_minutes"]["section"] == "Break screen"
    assert SECTIONS.index("Break screen") + 1 == SECTIONS.index("Login & site")


def test_nan_and_infinity_are_refused():
    for bad in (float("nan"), float("inf"), float("-inf")):
        assert validate_update({"http_port": bad}, {}) == ("http_port", "Must be a finite number")


def test_http_port_must_be_a_whole_number_in_range():
    for bad in (80, 70000, 8765.5):
        assert validate_update({"http_port": bad}, {"http_port": 8765}) is not None, bad
    assert validate_update({"http_port": 8080}, {"http_port": 8765}) is None
