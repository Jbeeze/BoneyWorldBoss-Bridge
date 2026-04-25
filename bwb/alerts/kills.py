"""BOSS_KILLED — pendingKills queue parser + reporter + cleanup.

Unique among alert handlers: kills are an array, deduped by (boss, timestamp)
in `state["reported_kills"]`, and the addon expects the bridge to REMOVE the
processed entry from SavedVariables so it isn't re-sent. The other alert
types use single overwriting snapshot fields with timestamp dedup.
"""
from __future__ import annotations

import re

from .. import shared
from ..state import save_state


def parse_savedvariables(path: str, verbose: bool = False) -> dict:
    """Parse the BoneyWorldBosses.lua SavedVariables file. Returns
    `{"pendingKills": [...]}` (other fields handled by their own modules)."""
    result: dict = {"pendingKills": []}

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError as e:
        print(f"[KILL] Error reading SavedVariables: {e}")
        return result

    if verbose:
        print(f"[KILL] SavedVariables file size: {len(content)} bytes")

    pending_start = content.find('["pendingKills"]')
    if pending_start == -1:
        if verbose:
            print("[KILL] Could not find pendingKills in SavedVariables")
        return result

    data_start = content.find('{', pending_start)
    if data_start == -1:
        return result

    # Find matching closing brace for the pendingKills table
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

    pending_content = content[data_start + 1:data_end]

    if verbose:
        print(f"[KILL] pendingKills content length: {len(pending_content)} chars")

    field_pattern = re.compile(
        r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))'
    )

    # Each kill entry is a balanced-brace block inside the array
    i = 0
    while i < len(pending_content):
        entry_start = pending_content.find('{', i)
        if entry_start == -1:
            break

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

        kill_str = pending_content[entry_start + 1:entry_end]
        i = entry_end + 1

        kill: dict = {}
        for field_match in field_pattern.finditer(kill_str):
            key = field_match.group(1)
            str_value = field_match.group(2)
            num_value = field_match.group(3)
            bool_value = field_match.group(4)

            if str_value is not None:
                kill[key] = str_value
            elif num_value is not None:
                if key == "timestamp":
                    kill[key] = int(float(num_value))
                else:
                    kill[key] = num_value
            elif bool_value is not None:
                if bool_value == "nil":
                    kill[key] = None
                else:
                    kill[key] = bool_value == "true"

        if "boss" in kill and "timestamp" in kill:
            result["pendingKills"].append(kill)
            if verbose:
                print(
                    f"[KILL] Parsed kill: {kill.get('testTargetName', kill.get('boss'))} "
                    f"at {kill.get('time')}"
                )
        elif kill:
            print(f"[KILL] Skipping incomplete kill record: {kill}")

    return result


def is_kill_already_reported(kill: dict, state: dict) -> bool:
    """Check if a kill has already been reported (key = boss_timestamp)."""
    reported_kills = state.get("reported_kills", [])
    kill_key = f"{kill.get('boss', '')}_{kill.get('timestamp', 0)}"
    return kill_key in reported_kills


def mark_kill_reported(kill: dict, state: dict) -> None:
    """Mark a kill as reported in state. Bounded to last 100 entries."""
    if "reported_kills" not in state:
        state["reported_kills"] = []

    kill_key = f"{kill.get('boss', '')}_{kill.get('timestamp', 0)}"
    state["reported_kills"].append(kill_key)

    if len(state["reported_kills"]) > 100:
        state["reported_kills"] = state["reported_kills"][-100:]


def remove_kill_from_savedvariables(kill: dict) -> bool:
    """Surgically remove a kill entry from pendingKills in the SavedVariables
    file so it isn't re-parsed on the next /reload. Returns True on success
    (or if the entry wasn't present)."""
    sv_file = shared.find_savedvariables_file()
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

    i = 0
    while i < len(pending_section):
        entry_start = pending_section.find('{', i)
        if entry_start == -1:
            break

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
            remove_start = entry_start
            remove_end = entry_end + 1
            # Consume trailing comma and whitespace
            while remove_end < len(pending_section) and pending_section[remove_end] in ' ,\t\n\r':
                remove_end += 1
            # Consume leading whitespace before the block
            while remove_start > 0 and pending_section[remove_start - 1] in ' \t':
                remove_start -= 1
            # Strip leading "[N] = " array index prefix
            prefix = pending_section[:remove_start].rstrip()
            idx_match = re.search(r'\[\d+\]\s*=\s*$', prefix)
            if idx_match:
                remove_start = len(prefix) - len(idx_match.group(0))
                while remove_start > 0 and pending_section[remove_start - 1] in ' \t\n\r':
                    remove_start -= 1

            new_pending = pending_section[:remove_start] + pending_section[remove_end:]
            new_content = content[:data_start] + new_pending + content[data_end + 1:]

            try:
                with open(sv_file, "w", encoding="utf-8") as f:
                    f.write(new_content)
                print("[KILL] Removed kill from SavedVariables")
                return True
            except IOError as e:
                print(f"[KILL] Error writing SavedVariables: {e}")
                return False

        i = entry_end + 1

    return True  # Not found (already removed)


def post_kill_report(kill: dict) -> bool:
    """Post a single kill to the bot. For test kills the alert carries the
    actual creature name (testTargetName) rather than a boss key."""
    boss_key = kill.get("boss", "unknown")
    is_test = kill.get("isTest", False)
    test_target = kill.get("testTargetName", "")

    if is_test:
        boss_name = test_target if test_target else "Unknown Creature"
    else:
        boss_name = shared._boss_display_names.get(boss_key, boss_key)

    alert = {
        "alertType": "BOSS_KILLED",
        "boss": boss_key,
        "time": kill.get("time", "?"),
        "date": kill.get("date", ""),
        "timestamp": kill.get("timestamp", 0),
        "layer": kill.get("layer", "?"),
        "layerId": kill.get("layerId", "?"),
        "msg": f"{boss_name} was killed!",
        "characterName": kill.get("characterName", ""),
    }

    if is_test:
        alert["isTest"] = True
        alert["testTargetName"] = test_target
        if kill.get("testNpcId"):
            alert["testNpcId"] = kill.get("testNpcId")

    log_prefix = "[TEST]" if is_test else "[KILL]"
    if is_test:
        npc_id = kill.get("testNpcId", "?")
        print(
            f"{log_prefix} Reporting: {boss_name} (NPC {npc_id}) at "
            f"{kill.get('time', '?')} ST, Layer {kill.get('layer', '?')} "
            f"({kill.get('layerId', '?')})"
        )
    else:
        print(
            f"{log_prefix} Reporting: {boss_name} at {kill.get('time', '?')} ST, "
            f"Layer {kill.get('layer', '?')} ({kill.get('layerId', '?')})"
        )

    return shared.post_to_bot(alert)


_checking_kills = False  # Prevent re-entry


def check_pending_kills(state: dict, verbose: bool = False) -> None:
    """Drain SavedVariables.pendingKills: post each unreported kill, mark it
    in state, and remove it from SavedVariables so it isn't seen again."""
    global _checking_kills

    if _checking_kills:
        return
    _checking_kills = True

    # Refresh cached character name (used by COMBAT_DETECTED elsewhere)
    shared.read_character_name()

    try:
        sv_file = shared.find_savedvariables_file()
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

        unreported = [k for k in pending_kills if not is_kill_already_reported(k, state)]

        if not unreported:
            if verbose:
                print(f"[KILL] All {len(pending_kills)} kill(s) already reported")
            return

        print(f"[KILL] Found {len(unreported)} NEW kill(s) to report (of {len(pending_kills)} total)")
        for i, kill in enumerate(unreported, 1):
            is_test = kill.get("isTest", False)
            boss = kill.get("testTargetName", kill.get("boss", "unknown")) if is_test else kill.get("boss", "unknown")
            prefix = "[TEST] " if is_test else ""
            print(
                f"[KILL]   {i}. {prefix}{boss} - {kill.get('time', '?')} ST - "
                f"Layer {kill.get('layer', '?')} ({kill.get('layerId', '?')})"
            )

        for kill in unreported:
            if post_kill_report(kill):
                mark_kill_reported(kill, state)
                save_state(state)
                remove_kill_from_savedvariables(kill)
                print("[KILL] Successfully reported kill")
            else:
                print("[KILL] Failed to report kill, will retry later")

        print("[KILL] Finished processing pending kills")

    finally:
        _checking_kills = False
