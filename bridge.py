#!/usr/bin/env python3
"""
Boney World Bosses - Discord Bridge v4.0
Reads all user configuration (guild id, discord id, bot api url, watched NPC ids,
boss display names) from the addon's SavedVariables file. No user-editable
constants live in this script.

Two detection modes:
  - Scout: Tails WoWCombatLog for real-time combat detection of bosses the
    addon tells us to watch.
  - Reporter: Reads SavedVariables for kill reports (requires /reload in-game).

Automatically finds the most recent combat log file.

This file is intentionally thin. Per-alertType parsing/forwarding lives in
bwb/alerts/*.py; shared globals + helpers are in bwb/shared.py; bridge state
is in bwb/state.py; combat-log parsing + dedup is in bwb/combat.py.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from bwb import shared
from bwb.combat import (
    find_latest_combat_log,
    get_file_info,
    parse_combat_line,
    resolve_layer_from_instance_id,
    should_alert,
)
from bwb.state import load_state, save_state
from bwb.alerts.callouts import check_callout_report
from bwb.alerts.char_profile import check_char_profile
from bwb.alerts.dbm import check_dbm_stats
from bwb.alerts.kills import check_pending_kills
from bwb.alerts.layers import check_layer_snapshot
from bwb.alerts.scout import check_scout_heartbeat, check_scout_report

BRIDGE_VERSION = "4.0.0"

BRIDGE_CONFIG_FILE = shared.SCRIPT_DIR / "bridge_config.json"


# =============================================================================
# BRIDGE CONFIG (logsDir cache)
# =============================================================================

def read_bridge_config() -> dict:
    """Load the bridge's operational config (currently just logsDir cache)."""
    if BRIDGE_CONFIG_FILE.exists():
        try:
            with open(BRIDGE_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {}


def write_bridge_config(cfg: dict) -> None:
    try:
        with open(BRIDGE_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except IOError as e:
        print(f"[WARN] Could not write {BRIDGE_CONFIG_FILE}: {e}")


def _logs_dir_in(root: Path) -> str:
    logs = root / "Logs"
    if logs.is_dir():
        return str(logs)
    return ""


def auto_detect_logs_dir() -> str:
    """Resolve the WoW Logs directory: cached path, then interactive prompt.

    On success, the resolved path is written to bridge_config.json so
    subsequent runs skip discovery."""
    cached = read_bridge_config().get("logsDir", "")
    if cached and Path(cached).is_dir():
        return cached

    print()
    print("[SETUP] First-run setup — tell the bridge where WoW is installed.")
    print("[SETUP] Paste the full path to your WoW flavor folder")
    print("[SETUP] (the folder containing Logs/ and WTF/). Examples:")
    print("[SETUP]   macOS:   /Applications/World of Warcraft/_anniversary_")
    print("[SETUP]   Windows: C:\\Program Files\\World of Warcraft\\_anniversary_")
    while True:
        try:
            entered = input("[SETUP] WoW install path: ").strip().strip('"').strip("'")
        except EOFError:
            return ""
        if not entered:
            print("[SETUP] Empty input. Try again, or Ctrl+C to abort.")
            continue
        resolved = _logs_dir_in(Path(entered))
        if resolved:
            _persist_logs_dir(resolved)
            return resolved
        print(f"[SETUP] No Logs/ folder at {entered}. Try again.")


def _persist_logs_dir(path: str) -> None:
    cfg = read_bridge_config()
    cfg["logsDir"] = path
    write_bridge_config(cfg)


# =============================================================================
# COMBAT LOG TAIL
# =============================================================================

def tail_log_file(state: dict):
    """Generator that yields new lines from the combat log file.

    Automatically finds and switches to the latest combat log file; handles
    file rotation (WoW creates a new file with /combatlog). Also drives the
    SavedVariables polling: every `KILL_REPORT_CHECK_INTERVAL` seconds it
    refreshes runtime config and walks the alert handlers."""
    current_log_path = None
    last_inode = state.get("last_inode", 0)
    last_pos = state.get("last_pos", 0)
    last_log_file = state.get("last_log_file", "")

    file_handle = None
    last_kill_check = time.time()  # Don't check immediately, main_loop already did
    last_heartbeat_check = time.time()

    while True:
        try:
            now = time.time()
            if now - last_kill_check >= shared.CONFIG["KILL_REPORT_CHECK_INTERVAL"]:
                # Refresh user config + watch tables + meta first so every
                # downstream post_to_bot call uses the freshest values.
                shared.reload_config_from_savedvars()
                check_pending_kills(state, verbose=False)
                check_layer_snapshot(state, verbose=False)
                check_scout_report(state, verbose=False)
                check_callout_report(state, verbose=False)
                check_dbm_stats(state, verbose=False)
                check_char_profile(state, verbose=False)
                last_kill_check = now

            if now - last_heartbeat_check >= shared.CONFIG["SCOUT_HEARTBEAT_CHECK_INTERVAL"]:
                check_scout_heartbeat(state)
                last_heartbeat_check = now

            latest_log = find_latest_combat_log()

            if not latest_log:
                if file_handle:
                    file_handle.close()
                    file_handle = None
                time.sleep(shared.CONFIG["POLL_INTERVAL"])
                continue

            if latest_log != current_log_path:
                if current_log_path is not None:
                    print("[TAIL] New combat log detected!")
                    print(f"[TAIL]   Old: {os.path.basename(current_log_path)}")
                    print(f"[TAIL]   New: {os.path.basename(latest_log)}")

                current_log_path = latest_log

                if current_log_path != last_log_file:
                    last_inode = 0
                    last_pos = 0

                if file_handle:
                    file_handle.close()
                    file_handle = None

            current_inode, current_size = get_file_info(current_log_path)

            file_rotated = False
            if current_inode != last_inode:
                if last_inode != 0:
                    print(f"[TAIL] File rotated (inode changed: {last_inode} -> {current_inode})")
                file_rotated = True
            elif current_size < last_pos:
                print(f"[TAIL] File rotated (size shrunk: {last_pos} -> {current_size})")
                file_rotated = True

            if file_rotated or file_handle is None:
                if file_handle:
                    file_handle.close()

                file_handle = open(current_log_path, "r", encoding="utf-8", errors="replace")

                if file_rotated and last_inode != 0:
                    file_handle.seek(0, 2)
                    last_pos = file_handle.tell()
                    print(f"[TAIL] Starting from end of file (pos {last_pos})")
                elif last_pos > 0:
                    file_handle.seek(last_pos)
                    print(f"[TAIL] Resumed from position {last_pos}")
                else:
                    file_handle.seek(0, 2)
                    last_pos = file_handle.tell()
                    print(f"[TAIL] Watching: {os.path.basename(current_log_path)}")
                    print(f"[TAIL] Starting from end (pos {last_pos})")

                last_inode = current_inode

                state["last_inode"] = last_inode
                state["last_pos"] = last_pos
                state["last_log_file"] = current_log_path
                save_state(state)

            while True:
                line = file_handle.readline()
                if line:
                    last_pos = file_handle.tell()
                    yield line
                else:
                    break

            state["last_pos"] = last_pos
            save_state(state)

            time.sleep(shared.CONFIG["POLL_INTERVAL"])

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[ERROR] Tail error: {e}")
            if file_handle:
                file_handle.close()
                file_handle = None
            time.sleep(shared.CONFIG["POLL_INTERVAL"])


# =============================================================================
# MAIN LOOP
# =============================================================================

def process_line(line: str) -> None:
    """Process a single combat log line; emit a COMBAT_DETECTED alert if it
    references a watched boss and we're past the per-boss dedup window."""
    result = parse_combat_line(line)
    if not result:
        return

    boss_name = result["boss_name"]
    if not should_alert(boss_name):
        return

    instance_id = result.get("instance_id", "")
    layer = resolve_layer_from_instance_id(instance_id)

    print(
        f"[ALERT] COMBAT_DETECTED: {boss_name} (NPC {result['npc_id']}) - "
        f"{result['event']} - Layer {layer} ({instance_id})"
    )

    now_epoch = int(time.time())
    now_time, now_date = shared.format_timestamp(now_epoch)

    alert = {
        "alertType": "COMBAT_DETECTED",
        "boss": boss_name,
        "npcId": result["npc_id"],
        "event": result["event"],
        "msg": f"{boss_name} detected in combat!",
        "channel": "combat_log",
        "characterName": shared._cached_character_name,
        "layer": layer,
        "layerId": instance_id,
        "time": now_time,
        "date": now_date,
        "timestamp": now_epoch,
    }
    shared.post_to_bot(alert)


def wait_for_addon_config() -> None:
    """Block until the addon has published guildId + discordId + botApiUrl.

    The addon flushes SavedVariables only on /reload or logout, so during this
    loop we're waiting for the user to (a) run `/bwb setup` in-game and (b)
    reload their UI."""
    announced_waiting = False
    while True:
        shared.reload_config_from_savedvars()
        if shared.is_runtime_config_complete():
            if announced_waiting:
                print("[WAIT] In-game setup detected. Resuming...")
            return
        if not announced_waiting:
            print("[WAIT] In-game setup not complete. Run |/bwb setup| in WoW")
            print("[WAIT] (and |/reload|) to supply Guild ID, Discord ID, and Bot API URL.")
            announced_waiting = True
        time.sleep(shared.CONFIG["WAIT_MESSAGE_INTERVAL"])


def main_loop() -> None:
    """Main processing loop."""
    print(f"[BoneyWorldBosses] Starting bridge v{BRIDGE_VERSION} (combat log + kill reports)...")
    print(f"  Logs dir: {shared.CONFIG['LOGS_DIR']}")
    print(f"  Poll interval: {shared.CONFIG['POLL_INTERVAL']}s")
    print(f"  Dedup window: {shared.CONFIG['DEDUP_WINDOW']}s")
    print(f"  Kill report check: every {shared.CONFIG['KILL_REPORT_CHECK_INTERVAL']}s")
    print()

    latest = find_latest_combat_log()
    if latest:
        print(f"[TAIL] Found combat log: {os.path.basename(latest)}")
    else:
        print("[TAIL] No combat log found yet. Run /combatlog in WoW to start one.")

    sv_file = shared.find_savedvariables_file()
    if sv_file:
        print(f"[KILL] Found SavedVariables: {os.path.basename(sv_file)}")
    else:
        print("[KILL] SavedVariables not found yet (will check after WoW login)")

    # Populate runtime config + watch tables from SavedVariables before
    # the first kill check. If user hasn't completed in-game setup, wait.
    shared.reload_config_from_savedvars()
    if not shared.is_runtime_config_complete():
        wait_for_addon_config()

    char_name = shared.read_character_name()
    if char_name:
        print(f"[CONFIG] Character: {char_name}")
    print(f"[CONFIG] Guild ID: {shared._runtime_config.get('guildId', '')}")
    print(f"[CONFIG] Bot API: {shared._runtime_config.get('botApiUrl', '')}")
    if shared._watched_npc_ids:
        print(f"[CONFIG] Watching NPC ids: {', '.join(sorted(shared._watched_npc_ids.keys()))}")
    if shared._cached_meta:
        print(
            f"[CONFIG] Addon version: {shared._cached_meta.get('addonVersion', 'unknown')} "
            f"(schema {shared._cached_meta.get('schemaVersion', '?')})"
        )
    print()

    state = load_state()

    # Recover from a crash / restart with an active scout. If the combat log
    # is stale, fire the synthetic off now so the channel doesn't keep
    # showing a phantom scout. If the log is fresh, this just refreshes the
    # heartbeat timestamps.
    if state.get("active_scout"):
        print("[SCOUT] Active scout found in persisted state; checking heartbeat...")
        check_scout_heartbeat(state)

    print("[KILL] Checking for pending kill reports...")
    check_pending_kills(state, verbose=True)

    print("[LAYER] Checking for layer snapshot...")
    check_layer_snapshot(state, verbose=True)

    print("[SCOUT] Checking for scout report...")
    check_scout_report(state, verbose=True)

    print("[DBM_STATS] Checking for DBM stats snapshot...")
    check_dbm_stats(state, verbose=True)

    print("[CHAR_PROFILE] Checking for character role snapshot...")
    check_char_profile(state, verbose=True)

    try:
        for line in tail_log_file(state):
            process_line(line)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Received interrupt, exiting...")


def resolve_logs_dir() -> bool:
    """Resolve LOGS_DIR via auto-detect + optional interactive prompt.

    User-owned config (Discord IDs, bot URL) is NOT validated here — that
    lives in the addon's SavedVariables and is handled by wait_for_addon_config."""
    if shared.CONFIG["LOGS_DIR"] and os.path.isdir(shared.CONFIG["LOGS_DIR"]):
        return True

    detected = auto_detect_logs_dir()
    if detected and os.path.isdir(detected):
        shared.CONFIG["LOGS_DIR"] = detected
        print(f"[CONFIG] Logs directory: {detected}")
        return True

    print("[CONFIG ERROR] Could not locate your WoW Logs directory.")
    return False


def _running_from_addons_folder() -> bool:
    """True if SCRIPT_DIR appears to sit inside a WoW AddOns folder."""
    return any(part.lower() == "addons" for part in shared.SCRIPT_DIR.parts)


if __name__ == "__main__":
    print("=" * 60)
    print(f"  Boney World Bosses - Bridge v{BRIDGE_VERSION}")
    print("  Scout: Combat log detection (real-time)")
    print("  Reporter: Kill reports (after /reload)")
    print("  Config + watch list read from SavedVariables")
    print("=" * 60)
    print()

    if _running_from_addons_folder():
        print("!" * 60)
        print("[MIGRATE] This bridge is running from inside an AddOns folder.")
        print("[MIGRATE] Starting with v4.0.0, the bridge is distributed")
        print("[MIGRATE] separately from the addon. Please download the latest")
        print("[MIGRATE] release and run it from any folder OUTSIDE AddOns/:")
        print("[MIGRATE]   https://github.com/Jbeeze/boneyworldboss-bridge/releases")
        print("!" * 60)
        print()

    if not resolve_logs_dir():
        sys.exit(1)

    main_loop()
