"""Per-alertType handlers. Each module exposes a `parse_*` (Lua → dict) and a
`check_*` (state, verbose) function. The polling loop in bridge.py calls each
`check_*` once per `KILL_REPORT_CHECK_INTERVAL` seconds. Dedup baselines live
in the bridge state dict (`last_*_timestamp` and `reported_kills`).
"""
