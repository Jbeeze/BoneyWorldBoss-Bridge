"""Cross-module state, helpers, and bot-side I/O for the bridge.

What lives here:
  - The two flavors of config: operational (CONFIG) and addon-supplied
    (_runtime_config + watch tables + meta) plus the loader that refreshes
    the latter from SavedVariables every poll cycle.
  - Cached strings/dicts (_cached_character_name, _watched_npc_ids,
    _boss_display_names, _cached_meta, _cached_layer_zones). Importers must
    access these via module attribute (e.g. `shared._cached_character_name`),
    not `from .shared import _cached_character_name`, because the loader
    reassigns them.
  - SavedVariables discovery + low-level Lua-table parsing primitives
    (_find_table_block, _parse_string_dict, _extract_balanced_table) that
    every alerts/* module needs.
  - format_timestamp and post_to_bot — used by every alert payload.
"""
from __future__ import annotations

import glob
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

# =============================================================================
# OPERATIONAL CONFIG — bridge-internal knobs; user values live in SavedVariables.
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

    # Seconds without combat-log mtime advance before bridge synthesizes a
    # SCOUT_REPORT(off). The addon enables LoggingCombat(true) while scouting,
    # so the file grows continuously while the player is in-world. Override
    # via env var for testing (e.g. BWB_SCOUT_HEARTBEAT_TIMEOUT=30).
    "SCOUT_HEARTBEAT_TIMEOUT": int(os.environ.get("BWB_SCOUT_HEARTBEAT_TIMEOUT", 300)),

    # How often to evaluate scout heartbeat staleness (seconds)
    "SCOUT_HEARTBEAT_CHECK_INTERVAL": 30,
}

# Max age (seconds) for a layer snapshot to be considered fresh
LAYER_STALENESS_WINDOW = 600  # 10 minutes

# Script directory. When bundled via PyInstaller --onefile, __file__ points at
# a temp extraction dir — which would lose bridge_config.json on every run. Use
# sys.executable instead so bridge_config.json sits next to the .exe/binary.
# Otherwise: this file is bwb/shared.py; project root is parent.parent.
if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent.parent


# =============================================================================
# CACHED STATE — mutated by reload_config_from_savedvars / read_character_name.
# =============================================================================

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


# =============================================================================
# TIMESTAMP FORMATTING
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
# SAVEDVARIABLES DISCOVERY + PARSING PRIMITIVES
# =============================================================================

def find_savedvariables_file() -> str | None:
    """Auto-discover BoneyWorldBosses.lua in WTF folder. Returns the most
    recently modified file if multiple accounts exist."""
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
    """Locate a `["<key>"] = { ... }` block and return the text BETWEEN its
    braces (exclusive). Brace-depth aware so nested tables are handled."""
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


def _extract_balanced_table(content: str, key_marker: str) -> str | None:
    """Find `key_marker` in `content` and return the substring from the next
    `{` through its matching `}` (INCLUSIVE). Returns None if marker isn't
    present or braces don't balance.

    Differs from `_find_table_block` only in that this returns the surrounding
    braces too — which makes it composable when you want to substring-match
    one nested block out of another."""
    idx = content.find(key_marker)
    if idx == -1:
        return None
    brace_open = content.find('{', idx)
    if brace_open == -1:
        return None
    depth = 0
    for i in range(brace_open, len(content)):
        if content[i] == '{':
            depth += 1
        elif content[i] == '}':
            depth -= 1
            if depth == 0:
                return content[brace_open:i + 1]
    return None


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
