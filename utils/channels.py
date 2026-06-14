"""
Runtime channel assignment.

Each bot function (Agenda & Achievements, Signups/Meetings, Calendar) needs to
know which Discord channel it lives in. Historically that came only from env
vars. This module lets a captain reassign a function to whatever channel they
run `/set_channel` in, and remembers it in a small JSON file so it survives
restarts. The env var stays as the default/fallback.
"""

import json
import os

from config import (
    AGENDA_ACHIEVEMENTS_CHANNEL_ID,
    SIGNUPS_CHANNEL_ID,
    CALENDAR_CHANNEL_ID,
)

_FILE = "channel_config.json"

# key → (human label, env-var default channel id)
FUNCTIONS: dict[str, dict] = {
    "agenda":   {"label": "Agenda & Achievements", "default": AGENDA_ACHIEVEMENTS_CHANNEL_ID},
    "signups":  {"label": "Auto Polls & Meetings",  "default": SIGNUPS_CHANNEL_ID},
    "calendar": {"label": "Calendar",               "default": CALENDAR_CHANNEL_ID},
}

_overrides: dict[str, int] = {}


def _load() -> None:
    global _overrides
    try:
        with open(_FILE) as f:
            data = json.load(f)
        _overrides = {k: int(v) for k, v in data.items() if k in FUNCTIONS}
    except Exception:
        _overrides = {}


def _save() -> None:
    try:
        with open(_FILE, "w") as f:
            json.dump(_overrides, f)
    except Exception as e:
        print(f"[Channels] Could not save {_FILE}: {e}")


def get_channel_id(key: str) -> int:
    """Channel id for a function — saved override first, then the env default."""
    if key in _overrides and _overrides[key]:
        return _overrides[key]
    return FUNCTIONS.get(key, {}).get("default", 0)


def set_channel(key: str, channel_id: int) -> None:
    if key not in FUNCTIONS:
        raise KeyError(key)
    _overrides[key] = int(channel_id)
    _save()


_load()
