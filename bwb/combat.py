"""Combat-log parsing + per-boss dedup + layer-id resolution + log discovery.

The bridge tails WoW's combat log to surface real-time COMBAT_DETECTED alerts
for the bosses the addon publishes via watchedNpcIds. This module owns the
parsing pipeline (raw line → structured dict) and the per-boss dedup window.
"""
from __future__ import annotations

import glob
import os
import time

from . import shared

# Combat events that indicate boss activity.
COMBAT_EVENTS = {
    "SPELL_CAST_START",
    "SPELL_CAST_SUCCESS",
    "SPELL_DAMAGE",
    "SWING_DAMAGE",
    "RANGE_DAMAGE",
    "SPELL_AURA_APPLIED",
}


# Combat log GUID format:
#   Creature-0-server-zone-instance-NPCID-spawn
# Index:    0    1    2     3      4      5     6


def extract_npc_id_from_guid(guid: str) -> str | None:
    """Extract NPC ID (index 5) from a creature GUID, or None."""
    if not guid or not guid.startswith("Creature-"):
        return None
    parts = guid.split("-")
    if len(parts) >= 6:
        return parts[5]
    return None


def extract_instance_id_from_guid(guid: str) -> str | None:
    """Extract instance ID (index 4) from a creature GUID, or None."""
    if not guid or not guid.startswith("Creature-"):
        return None
    parts = guid.split("-")
    if len(parts) >= 5:
        return parts[4]
    return None


def parse_combat_line(line: str) -> dict | None:
    """Parse a combat log line; return structured data if it references a
    watched boss (matched on NPC id), otherwise None.

    Watch tables (`shared._watched_npc_ids`, `shared._boss_display_names`)
    are populated by the addon via SavedVariables, so this module is
    boss-list agnostic."""
    line = line.strip()
    if not line:
        return None

    # Format: "M/D HH:MM:SS.mmm  EVENT,..."
    parts = line.split("  ", 1)  # Two spaces separate timestamp from data
    if len(parts) != 2:
        return None

    event_data = parts[1]
    if not event_data:
        return None

    fields = event_data.split(",")
    if len(fields) < 3:
        return None

    event_type = fields[0]
    if event_type not in COMBAT_EVENTS:
        return None

    source_guid = fields[1]
    source_name = fields[2].strip('"') if len(fields) > 2 else ""

    npc_id = extract_npc_id_from_guid(source_guid)
    if npc_id and npc_id in shared._watched_npc_ids:
        boss_key = shared._watched_npc_ids[npc_id]
        return {
            "boss_name": shared._boss_display_names.get(boss_key, boss_key),
            "boss_key": boss_key,
            "npc_id": npc_id,
            "event": event_type,
            "source_name": source_name,
            "instance_id": extract_instance_id_from_guid(source_guid) or "",
        }

    # Also check dest GUID for damage events (player attacking boss)
    if len(fields) >= 6:
        dest_guid = fields[4]
        dest_name = fields[5].strip('"') if len(fields) > 5 else ""

        npc_id = extract_npc_id_from_guid(dest_guid)
        if npc_id and npc_id in shared._watched_npc_ids:
            boss_key = shared._watched_npc_ids[npc_id]
            return {
                "boss_name": shared._boss_display_names.get(boss_key, boss_key),
                "boss_key": boss_key,
                "npc_id": npc_id,
                "event": event_type,
                "source_name": dest_name,
                "instance_id": extract_instance_id_from_guid(dest_guid) or "",
            }

    return None


# =============================================================================
# DEDUPLICATION (per-boss, time-windowed)
# =============================================================================

# Track last alert time per boss to avoid spam.
_last_alert_times: dict[str, float] = {}


def should_alert(boss_name: str) -> bool:
    """Check if we should send an alert for this boss (time-window dedup).

    Side-effect on success: bumps the last-alert timestamp for `boss_name`."""
    now = time.time()
    last_time = _last_alert_times.get(boss_name, 0)
    if now - last_time >= shared.CONFIG["DEDUP_WINDOW"]:
        _last_alert_times[boss_name] = now
        return True
    return False


# =============================================================================
# LAYER ID → LAYER NUMBER RESOLUTION
# =============================================================================

def resolve_layer_from_instance_id(instance_id: str) -> str:
    """Resolve an instance ID to a layer number using the cached layer
    snapshot (populated by alerts.layers.check_layer_snapshot)."""
    if not instance_id or not shared._cached_layer_zones:
        return "?"
    for map_id, layers in shared._cached_layer_zones.items():
        for layer_num, inst_id in layers.items():
            if inst_id == instance_id:
                return layer_num
    return "?"


# =============================================================================
# LOG FILE DISCOVERY (used by tail_log_file in bridge.py and by
# alerts.scout for heartbeat-mtime checks)
# =============================================================================

def find_latest_combat_log() -> str | None:
    """Find the most recent combat log file (`WoWCombatLog*.txt`) in the
    Logs directory. Returns full path or None."""
    logs_dir = shared.CONFIG["LOGS_DIR"]
    if not logs_dir or not os.path.isdir(logs_dir):
        return None
    pattern = os.path.join(logs_dir, "WoWCombatLog*.txt")
    log_files = glob.glob(pattern)
    if not log_files:
        return None
    return max(log_files, key=os.path.getmtime)


def get_file_info(path: str) -> tuple:
    """Get (inode, size) of `path`, or (0, 0) if missing."""
    try:
        stat = os.stat(path)
        return (stat.st_ino, stat.st_size)
    except OSError:
        return (0, 0)
