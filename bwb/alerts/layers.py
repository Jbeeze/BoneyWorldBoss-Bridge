"""LAYER_UPDATE — layerSnapshot snapshot parser + forwarder.

Side effect: also caches `zones` into `shared._cached_layer_zones` so
`combat.resolve_layer_from_instance_id` can map an instance id surfaced by a
COMBAT_DETECTED event to a layer number.
"""
from __future__ import annotations

import json
import re
import time

from .. import shared
from ..state import save_state


def parse_layer_snapshot(path: str, verbose: bool = False) -> dict | None:
    """Parse layerSnapshot from SavedVariables.

    Returns `{ timestamp, trigger, zones, characterName }` or None if missing
    or malformed (no zones table)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    snap_start = content.find('["layerSnapshot"]')
    if snap_start == -1:
        return None

    data_start = content.find('{', snap_start)
    if data_start == -1:
        return None

    brace_count = 0
    data_end = data_start
    for i in range(data_start, len(content)):
        if content[i] == '{':
            brace_count += 1
        elif content[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                data_end = i
                break

    snapshot_str = content[data_start:data_end + 1]

    ts_match = re.search(r'\["timestamp"\]\s*=\s*(\d+)', snapshot_str)
    timestamp = int(ts_match.group(1)) if ts_match else 0

    trigger_match = re.search(r'\["trigger"\]\s*=\s*"([^"]*)"', snapshot_str)
    trigger = trigger_match.group(1) if trigger_match else "unknown"

    char_match = re.search(r'\["characterName"\]\s*=\s*"([^"]*)"', snapshot_str)
    character_name = char_match.group(1) if char_match else ""

    zones_start = snapshot_str.find('["zones"]')
    if zones_start == -1:
        if verbose:
            print("[LAYER] No zones table in snapshot")
        return None

    zones_brace = snapshot_str.find('{', zones_start)
    if zones_brace == -1:
        return None

    brace_count = 0
    zones_end = zones_brace
    for i in range(zones_brace, len(snapshot_str)):
        if snapshot_str[i] == '{':
            brace_count += 1
        elif snapshot_str[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                zones_end = i
                break

    zones_str = snapshot_str[zones_brace:zones_end + 1]

    # Parse each zone: ["mapId"] = { ["layerNum"] = "instanceId", ... }
    zones: dict = {}
    zone_pattern = re.compile(r'\["(\d+)"\]\s*=\s*\{([^}]*)\}')
    for zone_match in zone_pattern.finditer(zones_str):
        map_id = zone_match.group(1)
        zone_content = zone_match.group(2)
        layers: dict = {}
        for layer_match in re.finditer(r'\["(\d+)"\]\s*=\s*"(\d+)"', zone_content):
            layers[layer_match.group(1)] = layer_match.group(2)
        zones[map_id] = layers

    if verbose:
        print(f"[LAYER] Parsed snapshot: trigger={trigger}, timestamp={timestamp}, {len(zones)} zone(s)")

    return {
        "timestamp": timestamp,
        "trigger": trigger,
        "zones": zones,
        "characterName": character_name,
    }


_checking_layers = False


def check_layer_snapshot(state: dict, verbose: bool = False) -> None:
    """Forward fresh layer snapshots as alertType=LAYER_UPDATE; cache zones
    for combat-side instance-id → layer-number resolution.

    Stale snapshots (older than `LAYER_STALENESS_WINDOW`) are skipped because
    a logout-queued snapshot replaying at bridge startup shouldn't overwrite
    the bot's current layer state."""
    global _checking_layers
    if _checking_layers:
        return
    _checking_layers = True

    try:
        sv_file = shared.find_savedvariables_file()
        if not sv_file:
            return

        snapshot = parse_layer_snapshot(sv_file, verbose=verbose)
        if not snapshot:
            if verbose:
                print("[LAYER] No layer snapshot found in SavedVariables")
            return

        # Always cache zones for instance ID → layer resolution in combat module
        if snapshot["zones"]:
            shared._cached_layer_zones = snapshot["zones"]

        last_ts = state.get("last_layer_timestamp", 0)
        if snapshot["timestamp"] <= last_ts:
            if verbose:
                print(f"[LAYER] Snapshot timestamp {snapshot['timestamp']} already sent (last: {last_ts})")
            return

        age = time.time() - snapshot["timestamp"]
        if age > shared.LAYER_STALENESS_WINDOW:
            if verbose:
                print(
                    f"[LAYER] Skipping stale snapshot ({int(age)}s old, "
                    f"threshold {shared.LAYER_STALENESS_WINDOW}s)"
                )
            return

        trigger = snapshot["trigger"]
        zones = snapshot["zones"]

        total_layers = sum(len(layers) for layers in zones.values())
        print(
            f"[LAYER] New layer snapshot detected (trigger: {trigger}, "
            f"{len(zones)} zone(s), {total_layers} mapping(s))"
        )

        for map_id, layers in sorted(zones.items()):
            layer_list = ", ".join(f"L{num}={inst}" for num, inst in sorted(layers.items()))
            print(f"[LAYER]   Zone {map_id}: {layer_list}")

        snap_time, snap_date = shared.format_timestamp(snapshot["timestamp"])

        alert = {
            "alertType": "LAYER_UPDATE",
            "trigger": trigger,
            "zones": zones,
            "characterName": snapshot.get("characterName", ""),
            "time": snap_time,
            "date": snap_date,
            "timestamp": snapshot["timestamp"],
        }

        print(f"[LAYER] Sending payload: {json.dumps(alert, indent=2)}")

        if shared.post_to_bot(alert):
            state["last_layer_timestamp"] = snapshot["timestamp"]
            save_state(state)
            print(
                f"[LAYER] Successfully reported layer update (trigger: {trigger}, "
                f"{len(zones)} zone(s), {total_layers} mapping(s))"
            )
        else:
            print("[LAYER] Failed to send layer snapshot, will retry")

    finally:
        _checking_layers = False
