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
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

BRIDGE_VERSION = "4.0.0"

# =============================================================================
# OPERATIONAL CONFIG — not user-editable; user values live in SavedVariables.
# =============================================================================
CONFIG = {
    # Path to WoW Logs directory (NOT the specific file!). Auto-detected or
    # cached in bridge_config.json; may also be asked interactively on first run.
    "LOGS_DIR": "",

    # Seconds between checking for new log lines
    "POLL_INTERVAL": 1,

    # Deduplication window in seconds (avoid spam from continuous combat)
    "DEDUP_WINDOW": 30,

    # How often to check SavedVariables for kill reports (seconds)
    "KILL_REPORT_CHECK_INTERVAL": 5,

    # How often to print the "waiting for in-game setup" message (seconds)
    "WAIT_MESSAGE_INTERVAL": 30,
}

# Script directory. When bundled via PyInstaller --onefile, __file__ points at
# a temp extraction dir — which would lose bridge_config.json on every run. Use
# sys.executable instead so bridge_config.json sits next to the .exe/binary.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).parent

STATE_FILE = SCRIPT_DIR / "bridge_state.json"
BRIDGE_CONFIG_FILE = SCRIPT_DIR / "bridge_config.json"

# Cached character name (read from SavedVariables)
_cached_character_name = ""

# Cached layer zones for instance ID → layer number lookup
# Format: { "map_id": { "layer_num": "instance_id" } }
_cached_layer_zones: dict = {}

# User-owned config, read from addon SavedVariables every poll cycle.
_runtime_config: dict = {
    "guildId": "",
    "discordId": "",
    "botApiUrl": "",
}

# Boss watch tables, driven by the addon. Shape:
#   _watched_npc_ids:     { "<npc_id>": "<boss_key>", ... }
#   _boss_display_names:  { "<boss_key>": "<display name>", ... }
_watched_npc_ids: dict = {}
_boss_display_names: dict = {}

# Meta breadcrumb (addon version / schema version) — forwarded as-is to bot.
_cached_meta: dict = {}


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
    """
    Resolve the WoW Logs directory via, in order:
      1. Cached path from bridge_config.json.
      2. Interactive prompt to the user.

    On success, the resolved path is written to bridge_config.json so
    subsequent runs skip discovery.
    """
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


# Combat events that indicate boss activity
COMBAT_EVENTS = {
    "SPELL_CAST_START",
    "SPELL_CAST_SUCCESS",
    "SPELL_DAMAGE",
    "SWING_DAMAGE",
    "RANGE_DAMAGE",
    "SPELL_AURA_APPLIED",
}

# Max age (seconds) for a layer snapshot to be considered fresh
LAYER_STALENESS_WINDOW = 600  # 10 minutes

# =============================================================================
# COMBAT LOG PARSING
# =============================================================================

# Combat log format:
# M/D HH:MM:SS.mmm  SUBEVENT,sourceGUID,sourceName,...
# GUID format: Creature-0-server-zone-instance-NPCID-spawn
# Example: Creature-0-6257-530-104772-18463-0000495DFA
#          [0]=Creature, [1]=0, [2]=server, [3]=zone, [4]=instance, [5]=npcId, [6]=spawn

def extract_npc_id_from_guid(guid: str) -> str | None:
    """
    Extract NPC ID from a creature GUID.
    GUID format: Creature-0-server-zone-instance-NPCID-spawn
    Returns: NPC ID string or None (index 5, 0-based)
    """
    if not guid or not guid.startswith("Creature-"):
        return None

    parts = guid.split("-")
    if len(parts) >= 6:
        return parts[5]  # NPC ID is at index 5
    return None


def extract_instance_id_from_guid(guid: str) -> str | None:
    """
    Extract instance ID from a creature GUID.
    GUID format: Creature-0-server-zone-instance-NPCID-spawn
    Returns: instance ID string or None (index 4, 0-based)
    """
    if not guid or not guid.startswith("Creature-"):
        return None

    parts = guid.split("-")
    if len(parts) >= 5:
        return parts[4]
    return None


def parse_combat_line(line: str) -> dict | None:
    """
    Parse a combat log line and return structured data if it contains a boss.
    Returns: {"boss_name": str, "event": str, "source_name": str} or None
    """
    line = line.strip()
    if not line:
        return None

    # Split timestamp from event data
    # Format: "M/D HH:MM:SS.mmm  EVENT,..."
    parts = line.split("  ", 1)  # Two spaces separate timestamp from data
    if len(parts) != 2:
        return None

    event_data = parts[1]
    if not event_data:
        return None

    # Split the CSV event data
    fields = event_data.split(",")
    if len(fields) < 3:
        return None

    event_type = fields[0]

    # Only process relevant combat events
    if event_type not in COMBAT_EVENTS:
        return None

    # Extract source GUID (field index 1) and source name (field index 2)
    source_guid = fields[1]
    source_name = fields[2].strip('"') if len(fields) > 2 else ""

    # Check if source is a boss the addon asked us to watch.
    npc_id = extract_npc_id_from_guid(source_guid)
    if npc_id and npc_id in _watched_npc_ids:
        boss_key = _watched_npc_ids[npc_id]
        return {
            "boss_name": _boss_display_names.get(boss_key, boss_key),
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
        if npc_id and npc_id in _watched_npc_ids:
            boss_key = _watched_npc_ids[npc_id]
            return {
                "boss_name": _boss_display_names.get(boss_key, boss_key),
                "boss_key": boss_key,
                "npc_id": npc_id,
                "event": event_type,
                "source_name": dest_name,
                "instance_id": extract_instance_id_from_guid(dest_guid) or "",
            }

    return None


# =============================================================================
# DEDUPLICATION
# =============================================================================

def format_timestamp(unix_ts: int | float) -> tuple[str, str]:
    """Convert a Unix timestamp to (time_str, date_str) e.g. ('1:39pm', '2026-04-15')."""
    dt = datetime.fromtimestamp(unix_ts)
    hour = dt.hour
    ampm = "am"
    if hour >= 12:
        ampm = "pm"
        if hour > 12:
            hour -= 12
    elif hour == 0:
        hour = 12
    time_str = f"{hour}:{dt.minute:02d}{ampm}"
    date_str = dt.strftime("%Y-%m-%d")
    return time_str, date_str


def resolve_layer_from_instance_id(instance_id: str) -> str:
    """Resolve an instance ID to a layer number using cached layer snapshot data."""
    if not instance_id or not _cached_layer_zones:
        return "?"
    for map_id, layers in _cached_layer_zones.items():
        for layer_num, inst_id in layers.items():
            if inst_id == instance_id:
                return layer_num
    return "?"


# Track last alert time per boss to avoid spam
_last_alert_times: dict[str, float] = {}


def should_alert(boss_name: str) -> bool:
    """Check if we should send an alert for this boss (deduplication)."""
    now = time.time()
    last_time = _last_alert_times.get(boss_name, 0)

    if now - last_time >= CONFIG["DEDUP_WINDOW"]:
        _last_alert_times[boss_name] = now
        return True

    return False


# =============================================================================
# BOT API
# =============================================================================

def post_to_bot(alert: dict) -> bool:
    """Post alert to bot API. Returns True on success."""
    bot_api_url = _runtime_config.get("botApiUrl", "")
    if not bot_api_url:
        print("[ERROR] No bot API URL configured!")
        return False

    api_url = bot_api_url.rstrip("/") + "/webhook/alert"
    alert["guildId"] = _runtime_config.get("guildId", "")
    alert["discordId"] = _runtime_config.get("discordId", "")
    # Forward the meta breadcrumb (addonVersion, schemaVersion) unmodified so
    # the bot sees exactly what the addon published.
    if _cached_meta:
        for key, value in _cached_meta.items():
            alert.setdefault(key, value)

    try:
        response = requests.post(api_url, json=alert, timeout=10)

        if response.status_code == 503:
            print("[ERROR] Bot not connected to Discord yet")
            return False

        if response.status_code not in (200, 201):
            print(f"[ERROR] Bot API returned {response.status_code}: {response.text}")
            return False

        result = response.json()
        print(f"[BOT] Alert sent to {result.get('channelsSent', 0)} channel(s)")
        return True

    except requests.RequestException as e:
        print(f"[ERROR] Network error: {e}")
        return False


# =============================================================================
# STATE MANAGEMENT
# =============================================================================

def load_state() -> dict:
    """Load the bridge state from file."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"last_inode": 0, "last_pos": 0, "reported_kills": [], "last_layer_timestamp": 0, "last_scout_timestamp": 0}


def save_state(state: dict) -> None:
    """Save the bridge state to file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# =============================================================================
# SAVEDVARIABLES PARSING
# =============================================================================

def find_savedvariables_file() -> str | None:
    """
    Auto-discover BoneyWorldBosses.lua in WTF folder.
    Returns the most recently modified file if multiple accounts exist.
    """
    logs_dir = CONFIG["LOGS_DIR"]
    if not logs_dir:
        return None

    # WTF folder is sibling to Logs folder: WoW/_anniversary_/WTF/Account/*/SavedVariables/
    wow_dir = Path(logs_dir).parent
    wtf_path = wow_dir / "WTF" / "Account"

    if not wtf_path.exists():
        return None

    # Search for BoneyWorldBosses.lua in any account folder
    pattern = str(wtf_path / "*" / "SavedVariables" / "BoneyWorldBosses.lua")
    files = glob.glob(pattern)

    if not files:
        return None

    # Return the most recently modified file
    return max(files, key=os.path.getmtime)


def read_character_name() -> str:
    """Read the top-level characterName from SavedVariables and cache it."""
    global _cached_character_name
    sv_file = find_savedvariables_file()
    if not sv_file:
        return _cached_character_name
    try:
        with open(sv_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        match = re.search(r'\["characterName"\]\s*=\s*"([^"]*)"', content)
        if match:
            _cached_character_name = match.group(1)
    except IOError:
        pass
    return _cached_character_name


def _find_table_block(content: str, key_marker: str) -> str | None:
    """Locate a `["<key>"] = { ... }` block and return the text between its braces.

    Uses brace-depth counting so nested tables are handled correctly.
    """
    start = content.find(key_marker)
    if start == -1:
        return None
    brace = content.find('{', start)
    if brace == -1:
        return None
    depth = 0
    for i in range(brace, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                return content[brace + 1:i]
    return None


def _parse_string_dict(block: str) -> dict:
    """Extract `["k"] = "v"` pairs from a flat Lua table body."""
    return {m.group(1): m.group(2)
            for m in re.finditer(r'\["([^"]+)"\]\s*=\s*"([^"]*)"', block)}


def reload_config_from_savedvars() -> bool:
    """Refresh user-owned config + watch tables + meta from the addon's
    SavedVariables file. Returns True if the file could be read."""
    global _watched_npc_ids, _boss_display_names, _cached_meta

    sv_file = find_savedvariables_file()
    if not sv_file:
        return False
    try:
        with open(sv_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return False

    config_block = _find_table_block(content, '["config"]')
    if config_block is not None:
        cfg_pairs = _parse_string_dict(config_block)
        for key in ("guildId", "discordId", "botApiUrl"):
            if key in cfg_pairs:
                _runtime_config[key] = cfg_pairs[key]

    watched_block = _find_table_block(content, '["watchedNpcIds"]')
    if watched_block is not None:
        _watched_npc_ids = _parse_string_dict(watched_block)

    names_block = _find_table_block(content, '["bossDisplayNames"]')
    if names_block is not None:
        _boss_display_names = _parse_string_dict(names_block)

    meta_block = _find_table_block(content, '["meta"]')
    if meta_block is not None:
        meta: dict = {}
        for m in re.finditer(
            r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+))', meta_block
        ):
            key = m.group(1)
            str_val = m.group(2)
            num_val = m.group(3)
            if str_val is not None:
                meta[key] = str_val
            elif num_val is not None:
                try:
                    meta[key] = int(num_val)
                except ValueError:
                    meta[key] = float(num_val)
        _cached_meta = meta

    return True


def is_runtime_config_complete() -> bool:
    """True when the addon has published all three required config values."""
    return bool(
        _runtime_config.get("guildId")
        and _runtime_config.get("discordId")
        and _runtime_config.get("botApiUrl")
    )


def parse_savedvariables(path: str, verbose: bool = False) -> dict:
    """
    Parse the BoneyWorldBosses.lua SavedVariables file.
    Returns a dict with pendingKills list.
    """
    result = {"pendingKills": []}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError as e:
        print(f"[KILL] Error reading SavedVariables: {e}")
        return result

    if verbose:
        print(f"[KILL] SavedVariables file size: {len(content)} bytes")

    # Find the pendingKills section by looking for it and extracting until ["config"]
    # or end of BoneyWorldBossesDB
    pending_start = content.find('["pendingKills"]')
    if pending_start == -1:
        if verbose:
            print("[KILL] Could not find pendingKills in SavedVariables")
        return result

    # Find where pendingKills data starts (after the = {)
    data_start = content.find('{', pending_start)
    if data_start == -1:
        return result

    # Find the end - look for },\n["config"] or },\n}
    # We need to find the matching closing brace
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

    pending_content = content[data_start+1:data_end]

    if verbose:
        print(f"[KILL] pendingKills content length: {len(pending_content)} chars")

    # Find each kill entry by matching balanced braces
    i = 0
    while i < len(pending_content):
        # Find start of an entry
        entry_start = pending_content.find('{', i)
        if entry_start == -1:
            break

        # Find matching close brace
        brace_count = 0
        entry_end = entry_start
        for j in range(entry_start, len(pending_content)):
            if pending_content[j] == '{':
                brace_count += 1
            elif pending_content[j] == '}':
                brace_count -= 1
                if brace_count == 0:
                    entry_end = j
                    break

        kill_str = pending_content[entry_start+1:entry_end]
        i = entry_end + 1

        # Extract fields
        kill = {}

        # Match ["key"] = "value" or ["key"] = number or ["key"] = true/false
        field_pattern = re.compile(r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))')

        for field_match in field_pattern.finditer(kill_str):
            key = field_match.group(1)
            str_value = field_match.group(2)
            num_value = field_match.group(3)
            bool_value = field_match.group(4)

            if str_value is not None:
                kill[key] = str_value
            elif num_value is not None:
                # Keep as string for layerId, convert to int for timestamp
                if key == "timestamp":
                    kill[key] = int(float(num_value))
                else:
                    kill[key] = num_value
            elif bool_value is not None:
                if bool_value == "nil":
                    kill[key] = None
                else:
                    kill[key] = bool_value == "true"

        # Only add if we have required fields
        if "boss" in kill and "timestamp" in kill:
            result["pendingKills"].append(kill)
            if verbose:
                print(f"[KILL] Parsed kill: {kill.get('testTargetName', kill.get('boss'))} at {kill.get('time')}")
        elif kill:
            print(f"[KILL] Skipping incomplete kill record: {kill}")

    return result


# =============================================================================
# KILL REPORT HANDLING
# =============================================================================

def is_kill_already_reported(kill: dict, state: dict) -> bool:
    """Check if a kill has already been reported."""
    reported_kills = state.get("reported_kills", [])

    # Create a unique key for this kill
    kill_key = f"{kill.get('boss', '')}_{kill.get('timestamp', 0)}"

    return kill_key in reported_kills


def mark_kill_reported(kill: dict, state: dict) -> None:
    """Mark a kill as reported in state."""
    if "reported_kills" not in state:
        state["reported_kills"] = []

    kill_key = f"{kill.get('boss', '')}_{kill.get('timestamp', 0)}"
    state["reported_kills"].append(kill_key)

    # Keep only last 100 reported kills to prevent unbounded growth
    if len(state["reported_kills"]) > 100:
        state["reported_kills"] = state["reported_kills"][-100:]


def remove_kill_from_savedvariables(kill: dict) -> bool:
    """
    Remove a kill entry from pendingKills in the SavedVariables file.
    Returns True on success.
    """
    sv_file = find_savedvariables_file()
    if not sv_file:
        return False

    try:
        with open(sv_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError as e:
        print(f"[KILL] Error reading SavedVariables for removal: {e}")
        return False

    boss = kill.get("boss", "")
    timestamp = kill.get("timestamp", 0)

    pending_start = content.find('["pendingKills"]')
    if pending_start == -1:
        return False

    data_start = content.find('{', pending_start)
    if data_start == -1:
        return False

    # Find the end of pendingKills
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

    pending_section = content[data_start:data_end + 1]

    # Find and remove the matching kill entry
    i = 0
    while i < len(pending_section):
        entry_start = pending_section.find('{', i)
        if entry_start == -1:
            break

        # Find matching close brace
        brace_count = 0
        entry_end = entry_start
        for j in range(entry_start, len(pending_section)):
            if pending_section[j] == '{':
                brace_count += 1
            elif pending_section[j] == '}':
                brace_count -= 1
                if brace_count == 0:
                    entry_end = j
                    break

        kill_block = pending_section[entry_start:entry_end + 1]

        boss_match = f'["boss"] = "{boss}"' in kill_block
        timestamp_match = f'["timestamp"] = {timestamp}' in kill_block

        if boss_match and timestamp_match:
            # Remove the entry and any surrounding whitespace/comma
            remove_start = entry_start
            remove_end = entry_end + 1
            # Consume trailing comma and whitespace
            while remove_end < len(pending_section) and pending_section[remove_end] in ' ,\t\n\r':
                remove_end += 1
            # Consume leading whitespace/index markers like [1] =
            while remove_start > 0 and pending_section[remove_start - 1] in ' \t':
                remove_start -= 1
            # Check for Lua array index like [1] = before the block
            prefix = pending_section[:remove_start].rstrip()
            idx_match = re.search(r'\[\d+\]\s*=\s*$', prefix)
            if idx_match:
                remove_start = len(prefix) - len(idx_match.group(0))
                # Also consume leading whitespace before index
                while remove_start > 0 and pending_section[remove_start - 1] in ' \t\n\r':
                    remove_start -= 1

            new_pending = pending_section[:remove_start] + pending_section[remove_end:]
            new_content = content[:data_start] + new_pending + content[data_end + 1:]

            try:
                with open(sv_file, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print(f"[KILL] Removed kill from SavedVariables")
                return True
            except IOError as e:
                print(f"[KILL] Error writing SavedVariables: {e}")
                return False

        i = entry_end + 1

    return True  # Not found (already removed)


def post_kill_report(kill: dict) -> bool:
    """Post a kill report to the Discord bot."""
    boss_key = kill.get("boss", "unknown")
    is_test = kill.get("isTest", False)
    test_target = kill.get("testTargetName", "")

    if is_test:
        # For test kills, use the actual creature name
        boss_name = test_target if test_target else "Unknown Creature"
    else:
        boss_name = _boss_display_names.get(boss_key, boss_key)

    alert = {
        "alertType": "BOSS_KILLED",
        "boss": boss_key,
        "time": kill.get("time", "?"),
        "date": kill.get("date", ""),
        "layer": kill.get("layer", "?"),
        "layerId": kill.get("layerId", "?"),
        "msg": f"{boss_name} was killed!",
        "characterName": kill.get("characterName", ""),
    }

    # Add test flag if this is a test kill
    if is_test:
        alert["isTest"] = True
        alert["testTargetName"] = test_target
        if kill.get("testNpcId"):
            alert["testNpcId"] = kill.get("testNpcId")

    log_prefix = "[TEST]" if is_test else "[KILL]"
    if is_test:
        npc_id = kill.get("testNpcId", "?")
        print(f"{log_prefix} Reporting: {boss_name} (NPC {npc_id}) at {kill.get('time', '?')} ST, Layer {kill.get('layer', '?')} ({kill.get('layerId', '?')})")
    else:
        print(f"{log_prefix} Reporting: {boss_name} at {kill.get('time', '?')} ST, Layer {kill.get('layer', '?')} ({kill.get('layerId', '?')})")

    return post_to_bot(alert)


_checking_kills = False  # Prevent re-entry

def check_pending_kills(state: dict, verbose: bool = False) -> None:
    """Check SavedVariables for pending kill reports and send them."""
    global _checking_kills

    # Prevent re-entry
    if _checking_kills:
        return
    _checking_kills = True

    # Refresh cached character name
    read_character_name()

    try:
        sv_file = find_savedvariables_file()

        if not sv_file:
            if verbose:
                print("[KILL] SavedVariables file not found")
            return

        if verbose:
            print(f"[KILL] Reading SavedVariables: {sv_file}")

        data = parse_savedvariables(sv_file, verbose=verbose)
        pending_kills = data.get("pendingKills", [])

        if verbose and not pending_kills:
            print("[KILL] No pending kills found in SavedVariables")

        if not pending_kills:
            return

        # Count unreported kills first
        unreported = [k for k in pending_kills if not is_kill_already_reported(k, state)]

        if not unreported:
            if verbose:
                print(f"[KILL] All {len(pending_kills)} kill(s) already reported")
            return

        # Log what we found
        print(f"[KILL] Found {len(unreported)} NEW kill(s) to report (of {len(pending_kills)} total)")
        for i, kill in enumerate(unreported, 1):
            is_test = kill.get("isTest", False)
            boss = kill.get("testTargetName", kill.get("boss", "unknown")) if is_test else kill.get("boss", "unknown")
            prefix = "[TEST] " if is_test else ""
            print(f"[KILL]   {i}. {prefix}{boss} - {kill.get('time', '?')} ST - Layer {kill.get('layer', '?')} ({kill.get('layerId', '?')})")

        for kill in unreported:
            if post_kill_report(kill):
                mark_kill_reported(kill, state)
                save_state(state)
                # Remove from SavedVariables so it won't be re-sent
                remove_kill_from_savedvariables(kill)
                print(f"[KILL] Successfully reported kill")
            else:
                print(f"[KILL] Failed to report kill, will retry later")

        print(f"[KILL] Finished processing pending kills")

    finally:
        _checking_kills = False


# =============================================================================
# LAYER SNAPSHOT HANDLING
# =============================================================================

def parse_layer_snapshot(path: str, verbose: bool = False) -> dict | None:
    """Parse layerSnapshot from SavedVariables. Returns dict or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    # Find layerSnapshot section
    snap_start = content.find('["layerSnapshot"]')
    if snap_start == -1:
        return None

    # Find the opening brace of the snapshot table
    data_start = content.find('{', snap_start)
    if data_start == -1:
        return None

    # Find matching closing brace using brace counting
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

    # Extract timestamp
    ts_match = re.search(r'\["timestamp"\]\s*=\s*(\d+)', snapshot_str)
    timestamp = int(ts_match.group(1)) if ts_match else 0

    # Extract trigger
    trigger_match = re.search(r'\["trigger"\]\s*=\s*"([^"]*)"', snapshot_str)
    trigger = trigger_match.group(1) if trigger_match else "unknown"

    # Extract characterName
    char_match = re.search(r'\["characterName"\]\s*=\s*"([^"]*)"', snapshot_str)
    character_name = char_match.group(1) if char_match else ""

    # Extract zones table
    zones_start = snapshot_str.find('["zones"]')
    if zones_start == -1:
        if verbose:
            print("[LAYER] No zones table in snapshot")
        return None

    # Find the zones table opening brace
    zones_brace = snapshot_str.find('{', zones_start)
    if zones_brace == -1:
        return None

    # Find matching closing brace for zones
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
    zones = {}
    zone_pattern = re.compile(r'\["(\d+)"\]\s*=\s*\{([^}]*)\}')
    for zone_match in zone_pattern.finditer(zones_str):
        map_id = zone_match.group(1)
        zone_content = zone_match.group(2)
        layers = {}
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
    """Check SavedVariables for new layer snapshot and send LAYER_UPDATE webhooks."""
    global _checking_layers
    if _checking_layers:
        return
    _checking_layers = True

    try:
        sv_file = find_savedvariables_file()
        if not sv_file:
            return

        snapshot = parse_layer_snapshot(sv_file, verbose=verbose)
        if not snapshot:
            if verbose:
                print("[LAYER] No layer snapshot found in SavedVariables")
            return

        # Always cache zones for instance ID → layer number lookup in combat detection
        global _cached_layer_zones
        if snapshot["zones"]:
            _cached_layer_zones = snapshot["zones"]

        last_ts = state.get("last_layer_timestamp", 0)
        if snapshot["timestamp"] <= last_ts:
            if verbose:
                print(f"[LAYER] Snapshot timestamp {snapshot['timestamp']} already sent (last: {last_ts})")
            return

        # Skip stale snapshots (e.g. bridge started before player logged in)
        age = time.time() - snapshot["timestamp"]
        if age > LAYER_STALENESS_WINDOW:
            print(f"[LAYER] Skipping stale snapshot ({int(age)}s old, threshold {LAYER_STALENESS_WINDOW}s)")
            return

        trigger = snapshot["trigger"]
        zones = snapshot["zones"]

        total_layers = sum(len(layers) for layers in zones.values())
        print(f"[LAYER] New layer snapshot detected (trigger: {trigger}, {len(zones)} zone(s), {total_layers} mapping(s))")

        for map_id, layers in sorted(zones.items()):
            layer_list = ", ".join(f"L{num}={inst}" for num, inst in sorted(layers.items()))
            print(f"[LAYER]   Zone {map_id}: {layer_list}")

        snap_time, snap_date = format_timestamp(snapshot["timestamp"])

        alert = {
            "alertType": "LAYER_UPDATE",
            "trigger": trigger,
            "zones": zones,
            "characterName": snapshot.get("characterName", ""),
            "time": snap_time,
            "date": snap_date,
        }

        print(f"[LAYER] Sending payload: {json.dumps(alert, indent=2)}")

        if post_to_bot(alert):
            state["last_layer_timestamp"] = snapshot["timestamp"]
            save_state(state)
            print(f"[LAYER] Successfully reported layer update (trigger: {trigger}, {len(zones)} zone(s), {total_layers} mapping(s))")
        else:
            print(f"[LAYER] Failed to send layer snapshot, will retry")

    finally:
        _checking_layers = False


# =============================================================================
# SCOUT REPORT HANDLING
# =============================================================================

def parse_scout_report(path: str, verbose: bool = False) -> dict | None:
    """Parse scoutReport from SavedVariables. Returns dict or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    report_start = content.find('["scoutReport"]')
    if report_start == -1:
        return None

    # Check for nil value (cleared report)
    nil_check = content[report_start:report_start + 50]
    if re.search(r'\["scoutReport"\]\s*=\s*nil', nil_check):
        return None

    data_start = content.find('{', report_start)
    if data_start == -1:
        return None

    # Find matching closing brace
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

    report_str = content[data_start:data_end + 1]

    # Extract fields using same regex as other parsers
    field_pattern = re.compile(r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))')
    report = {}
    for match in field_pattern.finditer(report_str):
        key = match.group(1)
        str_val = match.group(2)
        num_val = match.group(3)
        bool_val = match.group(4)
        if str_val is not None:
            report[key] = str_val
        elif num_val is not None:
            if key == "timestamp":
                report[key] = int(float(num_val))
            else:
                report[key] = num_val
        elif bool_val is not None:
            report[key] = bool_val == "true" if bool_val != "nil" else None

    if "action" not in report or "timestamp" not in report:
        if verbose:
            print("[SCOUT] Incomplete scout report, missing required fields")
        return None

    if verbose:
        print(f"[SCOUT] Parsed report: action={report.get('action')}, boss={report.get('boss', 'N/A')}, layer={report.get('layer', '?')}")

    return report


_checking_scout = False


def check_scout_report(state: dict, verbose: bool = False) -> None:
    """Check SavedVariables for new scout report and send SCOUT_REPORT webhook."""
    global _checking_scout
    if _checking_scout:
        return
    _checking_scout = True

    try:
        sv_file = find_savedvariables_file()
        if not sv_file:
            return

        report = parse_scout_report(sv_file, verbose=verbose)
        if not report:
            if verbose:
                print("[SCOUT] No scout report found in SavedVariables")
            return

        last_ts = state.get("last_scout_timestamp", 0)
        if report["timestamp"] <= last_ts:
            if verbose:
                print(f"[SCOUT] Report timestamp {report['timestamp']} already sent (last: {last_ts})")
            return

        action = report.get("action", "unknown")
        boss = report.get("boss", "")
        layer = report.get("layer", "?")
        layer_id = report.get("layerId", "?")
        character_name = report.get("characterName", "")

        boss_name = _boss_display_names.get(boss, boss)
        if action == "on":
            print(f"[SCOUT] New scout report: {character_name} scouting {boss_name} on Layer {layer} ({layer_id})")
        else:
            print(f"[SCOUT] Scout off report from {character_name} ({boss_name} L{layer})")

        scout_time, scout_date = format_timestamp(report["timestamp"])

        alert = {
            "alertType": "SCOUT_REPORT",
            "action": action,
            "boss": boss,
            "layer": layer,
            "layerId": layer_id,
            "characterName": character_name,
            "time": scout_time,
            "date": scout_date,
        }

        if post_to_bot(alert):
            state["last_scout_timestamp"] = report["timestamp"]
            save_state(state)
            print(f"[SCOUT] Successfully reported scout {action}")
        else:
            print(f"[SCOUT] Failed to send scout report, will retry")

    finally:
        _checking_scout = False


# =============================================================================
# CALLOUT REPORT
# =============================================================================

def parse_callout_report(path: str, verbose: bool = False) -> dict | None:
    """Parse calloutReport from SavedVariables. Returns dict or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    report_start = content.find('["calloutReport"]')
    if report_start == -1:
        return None

    # Check for nil value (cleared report)
    nil_check = content[report_start:report_start + 55]
    if re.search(r'\["calloutReport"\]\s*=\s*nil', nil_check):
        return None

    data_start = content.find('{', report_start)
    if data_start == -1:
        return None

    # Find matching closing brace
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

    report_str = content[data_start:data_end + 1]

    # Extract fields using same regex as other parsers
    field_pattern = re.compile(r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))')
    report = {}
    for match in field_pattern.finditer(report_str):
        key = match.group(1)
        str_val = match.group(2)
        num_val = match.group(3)
        bool_val = match.group(4)
        if str_val is not None:
            report[key] = str_val
        elif num_val is not None:
            if key == "timestamp":
                report[key] = int(float(num_val))
            else:
                report[key] = num_val
        elif bool_val is not None:
            report[key] = bool_val == "true" if bool_val != "nil" else None

    if "boss" not in report or "timestamp" not in report:
        if verbose:
            print("[CALLOUT] Incomplete callout report, missing required fields")
        return None

    if verbose:
        print(f"[CALLOUT] Parsed report: boss={report.get('boss')}, layer={report.get('layer', '?')}")

    return report


_checking_callout = False


def check_callout_report(state: dict, verbose: bool = False) -> None:
    """Check SavedVariables for new callout report and send CALLOUT webhook."""
    global _checking_callout
    if _checking_callout:
        return
    _checking_callout = True

    try:
        sv_file = find_savedvariables_file()
        if not sv_file:
            return

        report = parse_callout_report(sv_file, verbose=verbose)
        if not report:
            if verbose:
                print("[CALLOUT] No callout report found in SavedVariables")
            return

        last_ts = state.get("last_callout_timestamp", 0)
        if report["timestamp"] <= last_ts:
            if verbose:
                print(f"[CALLOUT] Report timestamp {report['timestamp']} already sent (last: {last_ts})")
            return

        boss = report.get("boss", "")
        layer = report.get("layer", "?")
        layer_id = report.get("layerId", "?")
        character_name = report.get("characterName", "")

        boss_name = _boss_display_names.get(boss, boss)
        print(f"[CALLOUT] New callout: {character_name} calling out {boss_name} on Layer {layer} ({layer_id})")

        callout_time, callout_date = format_timestamp(report["timestamp"])

        alert = {
            "alertType": "CALLOUT",
            "boss": boss,
            "layer": layer,
            "layerId": layer_id,
            "characterName": character_name,
            "time": callout_time,
            "date": callout_date,
        }

        if post_to_bot(alert):
            state["last_callout_timestamp"] = report["timestamp"]
            save_state(state)
            print(f"[CALLOUT] Successfully sent callout")
        else:
            print(f"[CALLOUT] Failed to send callout, will retry")

    finally:
        _checking_callout = False


# =============================================================================
# FILE TAILING
# =============================================================================

def find_latest_combat_log() -> str | None:
    """
    Find the most recent combat log file in the Logs directory.
    Combat logs are named: WoWCombatLog-MMDDYY_HHMMSS.txt
    Returns: Full path to the most recent file, or None if not found.
    """
    logs_dir = CONFIG["LOGS_DIR"]
    if not logs_dir or not os.path.isdir(logs_dir):
        return None

    # Find all combat log files
    pattern = os.path.join(logs_dir, "WoWCombatLog*.txt")
    log_files = glob.glob(pattern)

    if not log_files:
        return None

    # Return the most recently modified file
    return max(log_files, key=os.path.getmtime)


def get_file_info(path: str) -> tuple:
    """Get inode and size of file. Returns (inode, size) or (0, 0) if not found."""
    try:
        stat = os.stat(path)
        return (stat.st_ino, stat.st_size)
    except OSError:
        return (0, 0)


def tail_log_file(state: dict):
    """
    Generator that yields new lines from the combat log file.
    Automatically finds and switches to the latest combat log file.
    Handles file rotation (WoW creates new files with /combatlog).
    Also periodically checks for pending kill reports.
    """
    current_log_path = None
    last_inode = state.get("last_inode", 0)
    last_pos = state.get("last_pos", 0)
    last_log_file = state.get("last_log_file", "")

    file_handle = None
    last_kill_check = time.time()  # Don't check immediately, main_loop already did

    while True:
        try:
            # Periodically check for pending kill reports
            now = time.time()
            if now - last_kill_check >= CONFIG["KILL_REPORT_CHECK_INTERVAL"]:
                # Refresh user config + watch tables + meta first so every
                # downstream post_to_bot call uses the freshest values.
                reload_config_from_savedvars()
                check_pending_kills(state, verbose=False)
                check_layer_snapshot(state, verbose=False)
                check_scout_report(state, verbose=False)
                check_callout_report(state, verbose=False)
                last_kill_check = now

            # Find the latest combat log file
            latest_log = find_latest_combat_log()

            if not latest_log:
                if file_handle:
                    file_handle.close()
                    file_handle = None
                time.sleep(CONFIG["POLL_INTERVAL"])
                continue

            # Check if we switched to a new log file
            if latest_log != current_log_path:
                if current_log_path is not None:
                    print(f"[TAIL] New combat log detected!")
                    print(f"[TAIL]   Old: {os.path.basename(current_log_path)}")
                    print(f"[TAIL]   New: {os.path.basename(latest_log)}")

                current_log_path = latest_log

                # If this is a different file than last session, start fresh
                if current_log_path != last_log_file:
                    last_inode = 0
                    last_pos = 0

                if file_handle:
                    file_handle.close()
                    file_handle = None

            # Get current file info
            current_inode, current_size = get_file_info(current_log_path)

            # Detect file rotation (inode changed or file shrunk)
            file_rotated = False
            if current_inode != last_inode:
                if last_inode != 0:
                    print(f"[TAIL] File rotated (inode changed: {last_inode} -> {current_inode})")
                file_rotated = True
            elif current_size < last_pos:
                print(f"[TAIL] File rotated (size shrunk: {last_pos} -> {current_size})")
                file_rotated = True

            # Reopen file if rotated or not open
            if file_rotated or file_handle is None:
                if file_handle:
                    file_handle.close()

                file_handle = open(current_log_path, "r", encoding="utf-8", errors="replace")

                if file_rotated and last_inode != 0:
                    # Start from end to ignore history on rotation
                    file_handle.seek(0, 2)
                    last_pos = file_handle.tell()
                    print(f"[TAIL] Starting from end of file (pos {last_pos})")
                elif last_pos > 0:
                    # Resume from last known position
                    file_handle.seek(last_pos)
                    print(f"[TAIL] Resumed from position {last_pos}")
                else:
                    # New file, start from end
                    file_handle.seek(0, 2)
                    last_pos = file_handle.tell()
                    print(f"[TAIL] Watching: {os.path.basename(current_log_path)}")
                    print(f"[TAIL] Starting from end (pos {last_pos})")

                last_inode = current_inode

                # Save state
                state["last_inode"] = last_inode
                state["last_pos"] = last_pos
                state["last_log_file"] = current_log_path
                save_state(state)

            # Read new lines
            while True:
                line = file_handle.readline()
                if line:
                    last_pos = file_handle.tell()
                    yield line
                else:
                    break

            # Save position periodically
            state["last_pos"] = last_pos
            save_state(state)

            time.sleep(CONFIG["POLL_INTERVAL"])

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"[ERROR] Tail error: {e}")
            if file_handle:
                file_handle.close()
                file_handle = None
            time.sleep(CONFIG["POLL_INTERVAL"])


# =============================================================================
# MAIN LOOP
# =============================================================================

def process_line(line: str) -> None:
    """Process a single log line and send alerts if needed."""
    result = parse_combat_line(line)
    if not result:
        return

    boss_name = result["boss_name"]

    # Check deduplication
    if not should_alert(boss_name):
        return

    instance_id = result.get("instance_id", "")
    layer = resolve_layer_from_instance_id(instance_id)

    print(f"[ALERT] COMBAT_DETECTED: {boss_name} (NPC {result['npc_id']}) - {result['event']} - Layer {layer} ({instance_id})")

    now_time, now_date = format_timestamp(time.time())

    alert = {
        "alertType": "COMBAT_DETECTED",
        "boss": boss_name,
        "npcId": result["npc_id"],
        "event": result["event"],
        "msg": f"{boss_name} detected in combat!",
        "channel": "combat_log",
        "characterName": _cached_character_name,
        "layer": layer,
        "layerId": instance_id,
        "time": now_time,
        "date": now_date,
    }
    post_to_bot(alert)


def wait_for_addon_config() -> None:
    """Block until the addon has published guildId + discordId + botApiUrl.

    The addon flushes SavedVariables only on /reload or logout, so during this
    loop we're waiting for the user to (a) run `/bwb setup` in-game and (b)
    reload their UI.
    """
    announced_waiting = False
    while True:
        reload_config_from_savedvars()
        if is_runtime_config_complete():
            if announced_waiting:
                print("[WAIT] In-game setup detected. Resuming...")
            return
        if not announced_waiting:
            print("[WAIT] In-game setup not complete. Run |/bwb setup| in WoW")
            print("[WAIT] (and |/reload|) to supply Guild ID, Discord ID, and Bot API URL.")
            announced_waiting = True
        time.sleep(CONFIG["WAIT_MESSAGE_INTERVAL"])


def main_loop() -> None:
    """Main processing loop."""
    print(f"[BoneyWorldBosses] Starting bridge v{BRIDGE_VERSION} (combat log + kill reports)...")
    print(f"  Logs dir: {CONFIG['LOGS_DIR']}")
    print(f"  Poll interval: {CONFIG['POLL_INTERVAL']}s")
    print(f"  Dedup window: {CONFIG['DEDUP_WINDOW']}s")
    print(f"  Kill report check: every {CONFIG['KILL_REPORT_CHECK_INTERVAL']}s")
    print()

    # Show current combat log file
    latest = find_latest_combat_log()
    if latest:
        print(f"[TAIL] Found combat log: {os.path.basename(latest)}")
    else:
        print(f"[TAIL] No combat log found yet. Run /combatlog in WoW to start one.")

    # Check for SavedVariables file
    sv_file = find_savedvariables_file()
    if sv_file:
        print(f"[KILL] Found SavedVariables: {os.path.basename(sv_file)}")
    else:
        print(f"[KILL] SavedVariables not found yet (will check after WoW login)")

    # Populate runtime config + watch tables from SavedVariables before
    # the first kill check. If user hasn't completed in-game setup, wait.
    reload_config_from_savedvars()
    if not is_runtime_config_complete():
        wait_for_addon_config()

    char_name = read_character_name()
    if char_name:
        print(f"[CONFIG] Character: {char_name}")
    print(f"[CONFIG] Guild ID: {_runtime_config.get('guildId', '')}")
    print(f"[CONFIG] Bot API: {_runtime_config.get('botApiUrl', '')}")
    if _watched_npc_ids:
        print(f"[CONFIG] Watching NPC ids: {', '.join(sorted(_watched_npc_ids.keys()))}")
    if _cached_meta:
        print(f"[CONFIG] Addon version: {_cached_meta.get('addonVersion', 'unknown')} "
              f"(schema {_cached_meta.get('schemaVersion', '?')})")
    print()

    # Load state
    state = load_state()

    # Do initial kill report check (verbose)
    print("[KILL] Checking for pending kill reports...")
    check_pending_kills(state, verbose=True)

    # Do initial layer snapshot check (verbose)
    print("[LAYER] Checking for layer snapshot...")
    check_layer_snapshot(state, verbose=True)

    # Do initial scout report check (verbose)
    print("[SCOUT] Checking for scout report...")
    check_scout_report(state, verbose=True)

    try:
        for line in tail_log_file(state):
            process_line(line)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Received interrupt, exiting...")


def resolve_logs_dir() -> bool:
    """Resolve LOGS_DIR via auto-detect + optional interactive prompt.

    User-owned config (Discord IDs, bot URL) is NOT validated here — that
    lives in the addon's SavedVariables and is handled by wait_for_addon_config.
    """
    if CONFIG["LOGS_DIR"] and os.path.isdir(CONFIG["LOGS_DIR"]):
        return True

    detected = auto_detect_logs_dir()
    if detected and os.path.isdir(detected):
        CONFIG["LOGS_DIR"] = detected
        print(f"[CONFIG] Logs directory: {detected}")
        return True

    print("[CONFIG ERROR] Could not locate your WoW Logs directory.")
    return False


def _running_from_addons_folder() -> bool:
    """True if SCRIPT_DIR appears to sit inside a WoW AddOns folder."""
    return any(part.lower() == "addons" for part in SCRIPT_DIR.parts)


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
        print("[MIGRATE]   https://github.com/Jbeeze/BoneyWorldBoss-Bridge/releases")
        print("!" * 60)
        print()

    if not resolve_logs_dir():
        sys.exit(1)

    main_loop()
