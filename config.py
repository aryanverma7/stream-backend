"""
Config loading for the Mac Mini backend.

Per the project notes (Section 3): secrets live in a single local file that's
never committed anywhere, loaded once at startup. This is intentionally simple
for a single-user personal project - no secrets vault needed at this scale.
"""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"


class Config:
    """
    Thin wrapper around the config.json file. Values are read once at startup
    and held in memory - this is also the SAME object the admin dashboard
    (Task #4) will eventually read/write to make "changes take effect
    immediately without a restart" trivial, since dashboard and bot share one
    in-memory object rather than two separate processes needing to sync.
    """

    def __init__(self, path: Path = CONFIG_PATH):
        self._path = path
        self._data: dict = {}
        self.load()

    def load(self):
        if not self._path.exists():
            raise FileNotFoundError(
                f"config.json not found at {self._path}. "
                f"Copy config.example.json to config.json and fill in real values first."
            )
        with open(self._path, "r") as f:
            self._data = json.load(f)

    def save(self):
        """Used by the admin dashboard (Task #4) to persist edits made in the UI."""
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value):
        self._data[key] = value

    def all(self) -> dict:
        """Used by the admin dashboard's config editor - a safe copy, not the live dict."""
        return dict(self._data)


    # Explicit ALLOWLIST, not a blocklist - this is deliberate. A blocklist
    # would silently leak any new secret key added later if someone forgot
    # to add it to the blocklist. An allowlist only ever exposes keys
    # explicitly approved here, safe by default even as config.json grows.
    PUBLIC_SAFE_KEYS = ("social_links",)

    def public_safe(self) -> dict:
        """
        Used by the public-facing /api/public/site-config route (Task 6).
        NEVER returns anything outside PUBLIC_SAFE_KEYS - this is the one
        function allowed to hand config data to an unauthenticated route.
        """
        return {key: self._data[key] for key in self.PUBLIC_SAFE_KEYS if key in self._data}

    def __getitem__(self, key):
        return self._data[key]


# Single shared instance - imported by every other module in this backend.
config = Config()
