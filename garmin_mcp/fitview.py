"""FIT analysis on top of the vendored parser: the device's own truth.

Garmin's activity JSON is what Garmin computed on its servers; the FIT is what
the watch wrote. They disagree often enough to matter (elevation correction,
pauses, multisport legs, CIQ sensors). These helpers parse the original file
and shrink the result to something a model can hold.
"""

from __future__ import annotations

from dataclasses import asdict

from .fit import devfields, parser, streams

MAX_POINTS_DEFAULT = 120
MAX_POINTS_CAP = 500


def metrics(data: bytes) -> dict:
    """Session metrics straight from the file (fit_parser.FitMetrics)."""
    m = parser.parse_fit(data)
    d = {k: v for k, v in asdict(m).items() if v not in (None, [], {}, "")}
    if d.get("start_time") is not None:
        d["start_time"] = str(d["start_time"])
    d.pop("laps", None)          # laps come from get_activity_streams
    return d


def _resample(values: list, target: int) -> list:
    if len(values) <= target:
        return values
    stride = len(values) / target
    out = [values[min(len(values) - 1, int(i * stride))] for i in range(target)]
    out[-1] = values[-1]
    return out


def _stats(values: list) -> dict:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return {}
    return {"min": round(min(nums), 2), "max": round(max(nums), 2),
            "avg": round(sum(nums) / len(nums), 2)}


def stream_view(data: bytes, channels: list[str] | None = None,
                max_points: int = MAX_POINTS_DEFAULT,
                include_gps: bool = False) -> dict:
    """Downsampled time series. extract_streams already caps at 1500 points -
    still far too many to put in a prompt, so everything is resampled again to
    max_points (default 120) and each channel carries its min/max/avg."""
    max_points = max(10, min(int(max_points), MAX_POINTS_CAP))
    st = streams.extract_streams(data)
    keys = list(st["channels"])
    if channels:
        wanted = {c.strip().lower() for c in channels}
        keys = [k for k in keys if k.lower() in wanted]
    out = {
        "n_source_points": st["n"],
        "n_points": min(st["n"], max_points),
        "available_channels": list(st["channels"]),
        "t_s": [round(v, 1) if isinstance(v, (int, float)) else None
                for v in _resample(st["t"], max_points)],
        "channels": {k: {"values": _resample(st["channels"][k], max_points),
                         **_stats(st["channels"][k])} for k in keys},
        "laps": st["laps"],
        "legs": st["sessions"],
    }
    if include_gps:
        out["gps"] = _resample(st["gps"], max_points)
        out["bounds"] = st["bounds"]
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def swim_view(data: bytes) -> dict:
    """Pool swim detail: per length and per interval."""
    sw = streams.extract_swim(data)
    return {k: v for k, v in sw.items() if v not in (None, [], {})}


def sensors(data: bytes) -> list[dict]:
    """Connect-IQ / developer fields in the file, grouped per app - which
    external sensor (Stryd, Moxy/SmO2, CORE) actually recorded data."""
    out = []
    for app in devfields.list_dev_fields(data):
        out.append({
            "app": app.get("name") or app.get("uuid"),
            "produced_by": app.get("produced_by"),
            "fields": [{"name": f.get("name"), "units": f.get("units"),
                        "n": f.get("n"), "sample": f.get("sample"),
                        "looks_empty": f.get("looks_empty")}
                       for f in app.get("fields", [])],
        })
    return out
