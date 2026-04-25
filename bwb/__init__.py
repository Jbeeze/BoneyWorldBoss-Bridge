"""Boney World Bosses bridge — internal modules.

Importable submodules:
  bwb.shared             — globals (CONFIG, _runtime_config, watch tables, cached
                           character name, etc.) plus shared parsing helpers,
                           post_to_bot, format_timestamp, SavedVariables discovery,
                           and config reload.
  bwb.state              — bridge_state.json load/save.
  bwb.combat             — combat-log parsing, dedup, layer resolution, log file
                           discovery.
  bwb.alerts.kills       — parse + check_pending_kills + dedup/removal helpers.
  bwb.alerts.layers      — layerSnapshot.
  bwb.alerts.scout       — scoutReport + synthetic scout-off + heartbeat.
  bwb.alerts.callouts    — calloutReport.
  bwb.alerts.dbm         — dbmStats (DBM kill stats).
  bwb.alerts.char_profile — charProfile (per-character roles).

The bridge.py entrypoint orchestrates: it reads SavedVariables-derived config,
calls each alert module's check_* function on a poll cycle, tails the combat
log, and forwards combat-detected events.

Mutable cached globals (e.g. _cached_character_name, _watched_npc_ids) live on
bwb.shared and must be accessed via module attribute (`shared.X`) so a
reassignment inside reload_config_from_savedvars() is visible to every
importer; a `from bwb.shared import _cached_character_name` would freeze the
binding at import time.
"""
