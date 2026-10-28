"""fit v1.0.0 - reading FIT files: metrics, time series, developer fields,
best efforts within one file.

A MyCommons building block, copied into its users by MyCommons/sync.sh:
MyFITContainer (app/fit/) and Garmin MCP (garmin_mcp/fit/). Never edit a
copy; change it in MyCommons, run its tests, bump the version, sync again.
Modules: `parser` (parse_fit, FitMetrics, normalized_power), `streams`
(extract_streams, extract_swim, ...), `devfields` (list_dev_fields,
producer_from_dev_ids), `best_efforts` (peaks, fastest distances, splits).
Nothing here writes FIT files or keeps a cache; that stays in the apps.
"""

__version__ = "1.0.0"
