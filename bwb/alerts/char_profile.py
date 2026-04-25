"""CHAR_PROFILE — per-character role declarations (tank | healer | dps).

The roles sub-table is keyed on user-supplied character names, which can
contain characters outside the [A-Za-z0-9_] (`\\w`) class — so the roles entry
regex uses a permissive `[^"]+` pattern for the key while constraining the
value to a known set.
"""
from __future__ import annotations

import re

from .. import shared
from ..state import save_state

VALID_ROLES = {"tank", "healer", "dps"}


def parse_char_profile(path: str, verbose: bool = False) -> dict | None:
    """Parse the charProfile snapshot from SavedVariables.

    Shape: `{ timestamp, realm, roles = { [characterName] = role, ... } }`."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except IOError:
        return None

    block_start = content.find('["charProfile"]')
    if block_start == -1:
        return None
    if re.search(r'\["charProfile"\]\s*=\s*nil', content[block_start:block_start + 50]):
        return None

    outer = shared._extract_balanced_table(content[block_start:], '["charProfile"]')
    if outer is None:
        return None

    roles_block = shared._extract_balanced_table(outer, '["roles"]')
    top_only = outer.replace(roles_block, "{}") if roles_block else outer

    timestamp = None
    realm = ""
    for m in re.finditer(r'\["(\w+)"\]\s*=\s*(?:"([^"]*)"|([\d.]+))', top_only):
        key, sval, nval = m.group(1), m.group(2), m.group(3)
        if key == "timestamp" and nval is not None:
            try:
                timestamp = int(float(nval))
            except ValueError:
                pass
        elif key == "realm" and sval is not None:
            realm = sval

    if timestamp is None:
        if verbose:
            print("[CHAR_PROFILE] Missing timestamp in charProfile")
        return None

    roles: dict = {}
    if roles_block is not None:
        for m in re.finditer(r'\["([^"]+)"\]\s*=\s*"(\w+)"', roles_block):
            char_name, role = m.group(1), m.group(2)
            if role in VALID_ROLES:
                roles[char_name] = role

    if not roles:
        if verbose:
            print("[CHAR_PROFILE] No roles found in charProfile.roles")
        return None

    parsed = {
        "timestamp": timestamp,
        "realm": realm,
        "roles": roles,
    }
    if verbose:
        names = ", ".join(f"{k}:{v}" for k, v in roles.items())
        print(f"[CHAR_PROFILE] Parsed snapshot ({len(roles)} role(s)): {names}")
    return parsed


_checking_char_profile = False


def check_char_profile(state: dict, verbose: bool = False) -> None:
    """Forward a fresh character role snapshot as alertType=CHAR_PROFILE.

    Same dedup pattern as DBM_STATS. Skips silently when discordId isn't set."""
    global _checking_char_profile
    if _checking_char_profile:
        return
    _checking_char_profile = True

    try:
        if not shared._runtime_config.get("discordId"):
            if verbose:
                print("[CHAR_PROFILE] Skipping: discordId not configured")
            return

        sv_file = shared.find_savedvariables_file()
        if not sv_file:
            return

        snapshot = parse_char_profile(sv_file, verbose=verbose)
        if not snapshot:
            if verbose:
                print("[CHAR_PROFILE] No charProfile in SavedVariables")
            return

        last_ts = state.get("last_char_profile_timestamp", 0)
        if snapshot["timestamp"] <= last_ts:
            if verbose:
                print(f"[CHAR_PROFILE] Snapshot timestamp {snapshot['timestamp']} already sent (last: {last_ts})")
            return

        snap_time, snap_date = shared.format_timestamp(snapshot["timestamp"])
        alert = {
            "alertType": "CHAR_PROFILE",
            "realm": snapshot.get("realm", ""),
            "roles": snapshot["roles"],
            "time": snap_time,
            "date": snap_date,
            "timestamp": snapshot["timestamp"],
        }

        print(f"[CHAR_PROFILE] Forwarding {len(snapshot['roles'])} role(s)")

        if shared.post_to_bot(alert):
            state["last_char_profile_timestamp"] = snapshot["timestamp"]
            save_state(state)
            print("[CHAR_PROFILE] Successfully sent role snapshot")
        else:
            print("[CHAR_PROFILE] Failed to send, will retry on next poll")

    finally:
        _checking_char_profile = False
