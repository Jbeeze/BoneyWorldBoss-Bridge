"""Persistent bridge state: dedup baselines + active scout context.

Lives in bridge_state.json next to the executable. Default shape covers every
field the alerts/* modules look up via state.get(...) — alert modules use the
defensive .get() pattern, so older state files missing newer keys still work
on upgrade.
"""
from __future__ import annotations

import json

from . import shared

STATE_FILE = shared.SCRIPT_DIR / "bridge_state.json"


def load_state() -> dict:
    """Load the bridge state from file, or return defaults if missing."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {
        "last_inode": 0,
        "last_pos": 0,
        "reported_kills": [],
        "last_layer_timestamp": 0,
        "last_scout_timestamp": 0,
        "last_callout_timestamp": 0,
        "last_dbm_stats_timestamp": 0,
        "last_char_profile_timestamp": 0,
        # active_scout: dict while scouting, None otherwise. Shape:
        # { characterName, boss, layer, layerId, started_at,
        #   last_log_mtime (float), last_log_mtime_seen_at (float wall-clock) }
        "active_scout": None,
    }


def save_state(state: dict) -> None:
    """Save the bridge state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
