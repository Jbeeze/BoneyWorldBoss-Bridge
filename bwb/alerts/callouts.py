"""CALLOUT — calloutReport snapshot parser + forwarder."""
from __future__ import annotations

import re

from .. import shared
from ..state import save_state


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

    nil_check = content[report_start:report_start + 55]
    if re.search(r'\["calloutReport"\]\s*=\s*nil', nil_check):
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

    if "boss" not in report or "timestamp" not in report:
        if verbose:
            print("[CALLOUT] Incomplete callout report, missing required fields")
        return None

    if verbose:
        print(f"[CALLOUT] Parsed report: boss={report.get('boss')}, layer={report.get('layer', '?')}")

    return report


_checking_callout = False


def check_callout_report(state: dict, verbose: bool = False) -> None:
    """Forward a fresh calloutReport as alertType=CALLOUT."""
    global _checking_callout
    if _checking_callout:
        return
    _checking_callout = True

    try:
        sv_file = shared.find_savedvariables_file()
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

        boss_name = shared._boss_display_names.get(boss, boss)
        print(f"[CALLOUT] New callout: {character_name} calling out {boss_name} on Layer {layer} ({layer_id})")

        callout_time, callout_date = shared.format_timestamp(report["timestamp"])

        alert = {
            "alertType": "CALLOUT",
            "boss": boss,
            "layer": layer,
            "layerId": layer_id,
            "characterName": character_name,
            "time": callout_time,
            "date": callout_date,
            "timestamp": report["timestamp"],
        }

        if shared.post_to_bot(alert):
            state["last_callout_timestamp"] = report["timestamp"]
            save_state(state)
            print("[CALLOUT] Successfully sent callout")
        else:
            print("[CALLOUT] Failed to send callout, will retry")

    finally:
        _checking_callout = False
