"""DBM_STATS — per-character DBM kill stats snapshot parser + forwarder.

The dbmStats field is nested (top-level scalars + ["bosses"][bossKey] sub-
tables), so the flat field-regex used by the other parsers would collide on
duplicate `victories` keys across bosses. We slice the bosses sub-block out
before pulling top-level scalars, then parse each boss sub-table separately.
"""
from __future__ import annotations

import re

from .. import shared
from ..state import save_state


def parse_dbm_stats(path: str, verbose: bool = False) -> dict | None:
    """Parse the dbmStats snapshot from SavedVariables.

    Returns `{ timestamp, characterName, realm, bosses: {kazzak, doomwalker} }`
    or None if absent / nil / missing required fields."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    block_start = content.find('["dbmStats"]')
    if block_start == -1:
        return None
    if re.search(r'\["dbmStats"\]\s*=\s*nil', content[block_start:block_start + 50]):
        return None

    outer = shared._extract_balanced_table(content[block_start:], '["dbmStats"]')
    if outer is None:
        return None

    bosses_block = shared._extract_balanced_table(outer, '["bosses"]')
    top_only = outer.replace(bosses_block, "{}") if bosses_block else outer

    field_pattern = re.compile(
        r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+)|(true|false|nil))'
    )

    def pull_scalars(s: str) -> dict:
        out: dict = {}
        for m in field_pattern.finditer(s):
            key = m.group(1)
            sval, nval, bval = m.group(2), m.group(3), m.group(4)
            if sval is not None:
                out[key] = sval
            elif nval is not None:
                try:
                    out[key] = int(nval) if "." not in nval else float(nval)
                except ValueError:
                    out[key] = nval
            elif bval is not None:
                out[key] = (bval == "true") if bval != "nil" else None
        return out

    top = pull_scalars(top_only)

    timestamp = top.get("timestamp")
    if timestamp is None:
        if verbose:
            print("[DBM_STATS] Missing timestamp in dbmStats")
        return None

    bosses_out: dict = {}
    if bosses_block is not None:
        for boss_key in ("kazzak", "doomwalker"):
            sub = shared._extract_balanced_table(bosses_block, f'["{boss_key}"]')
            if sub is None:
                continue
            row = pull_scalars(sub)
            best = row.get("bestVictory")
            bosses_out[boss_key] = {
                "victories": int(row.get("victories") or 0),
                "wipes": int(row.get("wipes") or 0),
                "bestVictory": float(best) if isinstance(best, (int, float)) else None,
            }

    if not bosses_out:
        if verbose:
            print("[DBM_STATS] No boss sub-tables parsed from dbmStats")
        return None

    parsed = {
        "timestamp": int(timestamp) if isinstance(timestamp, (int, float)) else int(float(timestamp)),
        "characterName": top.get("characterName", ""),
        "realm": top.get("realm", ""),
        "bosses": bosses_out,
    }

    if verbose:
        k = bosses_out.get("kazzak", {})
        d = bosses_out.get("doomwalker", {})
        print(
            f"[DBM_STATS] Parsed snapshot for {parsed['characterName']}: "
            f"kazzak V:{k.get('victories', 0)}/W:{k.get('wipes', 0)}, "
            f"doomwalker V:{d.get('victories', 0)}/W:{d.get('wipes', 0)}"
        )
    return parsed


_checking_dbm_stats = False


def check_dbm_stats(state: dict, verbose: bool = False) -> None:
    """Forward a fresh DBM stats snapshot to the bot as alertType=DBM_STATS.

    Dedup by snapshot timestamp (same pattern as layerSnapshot/scoutReport).
    Skips silently if discordId isn't configured yet."""
    global _checking_dbm_stats
    if _checking_dbm_stats:
        return
    _checking_dbm_stats = True

    try:
        if not shared._runtime_config.get("discordId"):
            if verbose:
                print("[DBM_STATS] Skipping: discordId not configured")
            return

        sv_file = shared.find_savedvariables_file()
        if not sv_file:
            return

        snapshot = parse_dbm_stats(sv_file, verbose=verbose)
        if not snapshot:
            if verbose:
                print("[DBM_STATS] No DBM stats snapshot in SavedVariables")
            return

        last_ts = state.get("last_dbm_stats_timestamp", 0)
        if snapshot["timestamp"] <= last_ts:
            if verbose:
                print(f"[DBM_STATS] Snapshot timestamp {snapshot['timestamp']} already sent (last: {last_ts})")
            return

        snap_time, snap_date = shared.format_timestamp(snapshot["timestamp"])
        alert = {
            "alertType": "DBM_STATS",
            "characterName": snapshot.get("characterName", ""),
            "realm": snapshot.get("realm", ""),
            "bosses": snapshot.get("bosses", {}),
            "time": snap_time,
            "date": snap_date,
            "timestamp": snapshot["timestamp"],
        }

        print(f"[DBM_STATS] Forwarding snapshot for {snapshot.get('characterName', '?')}")

        if shared.post_to_bot(alert):
            state["last_dbm_stats_timestamp"] = snapshot["timestamp"]
            save_state(state)
            print("[DBM_STATS] Successfully sent snapshot")
        else:
            print("[DBM_STATS] Failed to send, will retry on next poll")

    finally:
        _checking_dbm_stats = False
