"""SCOUT_REPORT — on/off snapshot + synthetic-off heartbeat.

Two layers of state work together here:
  - `scoutReport` snapshot in SavedVariables (timestamp-deduped) is the
    user-driven on/off signal.
  - `state["active_scout"]` retains the boss/layer/character context plus a
    combat-log mtime baseline so a player who disconnects without a clean
    /bwb scout off can be auto-reported "off" once their combat log goes
    stale (`SCOUT_HEARTBEAT_TIMEOUT`).
"""
from __future__ import annotations

import os
import re
import time

from .. import shared
from ..combat import find_latest_combat_log
from ..state import save_state


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

    # Cleared snapshot
    nil_check = content[report_start:report_start + 50]
    if re.search(r'\["scoutReport"\]\s*=\s*nil', nil_check):
        return None

    data_start = content.find('{', report_start)
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

    report_str = content[data_start:data_end + 1]

    field_pattern = re.compile(
        r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))'
    )
    report: dict = {}
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
        print(
            f"[SCOUT] Parsed report: action={report.get('action')}, "
            f"boss={report.get('boss', 'N/A')}, layer={report.get('layer', '?')}"
        )

    return report


_checking_scout = False


def check_scout_report(state: dict, verbose: bool = False) -> None:
    """Forward a fresh scoutReport as alertType=SCOUT_REPORT.

    On `action == "on"`: snapshot the active-scout context (character, boss,
    layer, log mtime baseline) into state so a later disconnect still has
    something to synthesize a clean SCOUT_REPORT(off) from."""
    global _checking_scout
    if _checking_scout:
        return
    _checking_scout = True

    try:
        sv_file = shared.find_savedvariables_file()
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

        boss_name = shared._boss_display_names.get(boss, boss)
        if action == "on":
            print(f"[SCOUT] New scout report: {character_name} scouting {boss_name} on Layer {layer} ({layer_id})")
        else:
            print(f"[SCOUT] Scout off report from {character_name} ({boss_name} L{layer})")

        scout_time, scout_date = shared.format_timestamp(report["timestamp"])

        alert = {
            "alertType": "SCOUT_REPORT",
            "action": action,
            "boss": boss,
            "layer": layer,
            "layerId": layer_id,
            "characterName": character_name,
            "time": scout_time,
            "date": scout_date,
            "timestamp": report["timestamp"],
        }

        if shared.post_to_bot(alert):
            state["last_scout_timestamp"] = report["timestamp"]
            if action == "on":
                # Capture context now while the player is online, so a later
                # synthetic scout-off (after combat log goes stale) has fresh
                # character/boss/layer to report.
                log_path = find_latest_combat_log()
                try:
                    log_mtime = os.path.getmtime(log_path) if log_path else 0.0
                except OSError:
                    log_mtime = 0.0
                state["active_scout"] = {
                    "characterName": character_name,
                    "boss": boss,
                    "layer": layer,
                    "layerId": layer_id,
                    "started_at": report["timestamp"],
                    "last_log_mtime": log_mtime,
                    "last_log_mtime_seen_at": time.time(),
                }
            else:
                state["active_scout"] = None
            save_state(state)
            print(f"[SCOUT] Successfully reported scout {action}")
        else:
            print("[SCOUT] Failed to send scout report, will retry")

    finally:
        _checking_scout = False


def emit_synthetic_scout_off(state: dict, reason: str = "heartbeat_stale") -> bool:
    """Send a synthetic SCOUT_REPORT(off) when the scouting player's combat
    log has gone stale.

    Sources character/boss/layer from `state["active_scout"]` because the
    player is offline; SavedVariables won't reflect a fresh off."""
    ctx = state.get("active_scout")
    if not ctx:
        return False

    now_epoch = int(time.time())
    scout_time, scout_date = shared.format_timestamp(now_epoch)

    boss = ctx.get("boss", "")
    alert = {
        "alertType": "SCOUT_REPORT",
        "action": "off",
        "boss": boss,
        "layer": ctx.get("layer", "?"),
        "layerId": ctx.get("layerId", "?"),
        "characterName": ctx.get("characterName", ""),
        "time": scout_time,
        "date": scout_date,
        "timestamp": now_epoch,
        "synthetic": True,
        "syntheticReason": reason,
    }

    boss_display = shared._boss_display_names.get(boss, boss)
    print(
        f"[SCOUT] Synthetic scout-off ({reason}): {ctx.get('characterName', '?')} "
        f"({boss_display} L{ctx.get('layer', '?')})"
    )

    if shared.post_to_bot(alert):
        # Bump dedup baseline so a stale on-report still in SavedVariables
        # (older than now_epoch) cannot re-trigger check_scout_report.
        state["last_scout_timestamp"] = max(state.get("last_scout_timestamp", 0), now_epoch)
        state["active_scout"] = None
        save_state(state)
        return True
    print("[SCOUT] Failed to send synthetic scout-off, will retry next tick")
    return False


def check_scout_heartbeat(state: dict) -> None:
    """If a scout is active, watch the combat log mtime. Stale beyond
    SCOUT_HEARTBEAT_TIMEOUT seconds → fire a synthetic SCOUT_REPORT(off)."""
    ctx = state.get("active_scout")
    if not ctx:
        return

    log_path = find_latest_combat_log()
    now = time.time()
    mtime = None
    if log_path:
        try:
            mtime = os.path.getmtime(log_path)
        except OSError:
            mtime = None

    if mtime is not None and mtime > ctx.get("last_log_mtime", 0.0):
        ctx["last_log_mtime"] = mtime
        ctx["last_log_mtime_seen_at"] = now
        state["active_scout"] = ctx
        save_state(state)
        return

    last_seen = ctx.get("last_log_mtime_seen_at", now)
    if now - last_seen >= shared.CONFIG["SCOUT_HEARTBEAT_TIMEOUT"]:
        emit_synthetic_scout_off(state, reason="heartbeat_stale")
