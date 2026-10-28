"""Time series (record streams) from a FIT file for the detail/chart view.

Reads the record messages and returns downsampled arrays per channel plus the
GPS polyline + bounding box. Read-only operation; the original stays untouched.
Missing channels/broken files yield empty results (no exception)."""

from __future__ import annotations

import bisect
import io
import math
import warnings
from datetime import datetime, timedelta

import fitdecode

from . import best_efforts
from .parser import _SPORT_NAMES, _SUB_SPORT_NAMES, _enum_name

_SEMI = 180.0 / 2**31           # semicircles -> degrees
_MAX_POINTS = 1500              # target resolution after downsampling

# Display labels per session sport (multisport legs)
_LEG_LABELS = {"swimming": "Swim", "cycling": "Bike", "running": "Run"}

# Known channels: key -> (FIT field candidates in priority order, factor). These are
# curated (label/color/scaling, field name aliases per device). ALL other
# numeric record fields are additionally detected AUTOMATICALLY as generic channels
# (device-independent) — see _build_plan. Developer fields (Moxy/HRV/
# Core/Stryd) appear in fitdecode under their field name like native fields.
_KNOWN = (
    ("hr",          ("heart_rate",), 1.0),
    ("power",       ("power",), 1.0),
    ("cadence",     ("cadence", "running_cadence"), 1.0),
    ("altitude",    ("enhanced_altitude", "altitude"), 1.0),
    ("speed",       ("enhanced_speed", "speed"), 3.6),     # m/s -> km/h
    ("distance",    ("distance",), 0.001),                 # m -> km (x axis)
    # extended streams; field names vary per sensor/encoder (native vs.
    # developer field), hence several candidates.
    ("smo2",        ("SmO2", "saturated_hemoglobin_percent", "currHemoPerc"), 1.0),  # muscle O2 (%)
    ("hb_diff",     ("HbDiff", "total_hemoglobin_conc"), 1.0),       # Hb diff/tHb (g/dl)
    ("core_temp",   ("core_temperature",), 1.0),           # core body temp (°C)
    ("skin_temp",   ("SkinTemperature", "skin_temperature"), 1.0),   # skin temp (°C)
    ("temperature", ("temperature",), 1.0),                # ambient (°C)
    ("heat_strain", ("heat_strain_index",), 1.0),          # heat strain
    ("rmssd",       ("RMSSD",), 1.0),                       # HRV (ms)
    ("sdnn",        ("SDNN",), 1.0),                        # HRV (ms)
    ("dfa_a1",      ("Alpha1",), 1.0),                      # DFA alpha1
)

# Field names already taken by a known channel:
_KNOWN_FIELDS = {c for _k, cands, _f in _KNOWN for c in cands}
# Numeric, but not meaningful as a curve / handled elsewhere (GPS, time,
# monotonically accumulating, enums):
_SKIP_FIELDS = {"timestamp", "position_lat", "position_long",
                "accumulated_power", "fractional_cadence", "activity_type"}

# Curated label/color per known channel key (for series_meta).
_META = {
    "hr": ("Heart rate (bpm)", "#ff5d57"),
    "power": ("Power (W)", "#f0a23a"),
    "cadence": ("Cadence", "#7b6cff"),
    "altitude": ("Altitude (m)", "#35c08a"),
    "speed": ("Speed (km/h)", "#4f8cff"),
    "pace": ("Pace (min/km)", "#4f8cff"),       # derived from speed by the detail JS
    "smo2": ("SmO₂ (%)", "#e35d8a"),
    "hb_diff": ("Hb diff (g/dl)", "#c0566e"),
    "core_temp": ("Core temperature (°C)", "#ff8a3d"),
    "skin_temp": ("Skin temperature (°C)", "#f0a23a"),
    "temperature": ("Temperature (°C)", "#9aa7b6"),
    "heat_strain": ("Heat strain index", "#ff6b3d"),
    "rmssd": ("HRV RMSSD (ms)", "#7b6cff"),
    "sdnn": ("HRV SDNN (ms)", "#9b8cff"),
    "dfa_a1": ("DFA α1", "#35c0b0"),
}
# Color palette for automatically detected (unknown) channels.
_PALETTE = ("#e8896b", "#6bbf8a", "#8a7bff", "#d98ad0", "#5bb0c9",
            "#c9a35b", "#a0a8b4", "#d06b6b", "#6bb0d0", "#b0c95b")


def _humanize(name: str) -> str:
    s = name.replace("_", " ").strip()
    return s if (s[:1].isupper()) else (s[:1].upper() + s[1:])


def _build_plan(recs: list[dict]) -> list[tuple]:
    """Channel plan for THIS file: known channels (with present candidates)
    + all other numeric record fields as generic channels. This way streams
    are detected automatically, independent of the device."""
    present: set[str] = set()
    for r in recs:
        for k, v in r.items():
            if isinstance(v, (int, float)):
                present.add(k)
    plan: list[tuple] = []
    for key, cands, factor in _KNOWN:
        here = tuple(c for c in cands if c in present)
        if here:
            plan.append((key, here, factor))
    for f in sorted(present - _KNOWN_FIELDS - _SKIP_FIELDS):
        plan.append((f, (f,), 1.0))                # generic, factor 1.0
    return plan


def series_meta(keys) -> list[dict]:
    """[{key,label,color}] for the channel keys to draw (order as passed).
    Known keys curated, unknown ones humanized + a palette color."""
    out: list[dict] = []
    pi = 0
    for k in keys:
        if k in _META:
            label, color = _META[k]
        else:
            label, color = _humanize(k), _PALETTE[pi % len(_PALETTE)]
            pi += 1
        out.append({"key": k, "label": label, "color": color})
    return out


# Groups + order for the line selection boxes (detail chart).
_CHART_GROUPS = [
    ("Heart & HRV", ["hr", "rmssd", "sdnn", "dfa_a1"]),
    ("Power & Pace", ["power", "pace", "speed", "cadence"]),
    ("Oxygen & Temperature", ["smo2", "hb_diff", "core_temp", "skin_temp",
                              "temperature", "heat_strain"]),
    ("Elevation", ["altitude"]),
]
# Running dynamics: detected by field name (case-insensitive, auto-detected keys
# carry the FIT field name like "Air Power"/"stance_time").
_RUNNING_DYNAMICS = {
    "air power", "form power", "effort pace", "leg spring stiffness",
    "impact loading rate", "stance time", "step length", "vertical oscillation",
    "vertical ratio", "muscle state", "muscle trend", "ground time",
    "ground contact time", "vertical_oscillation", "vertical_ratio",
    "stance_time", "step_length",
}
_KEY_GROUP = {k: title for title, keys in _CHART_GROUPS for k in keys}
_GROUP_ORDER = [t for t, _ in _CHART_GROUPS] + ["Running dynamics", "Other"]


def _chart_group(key: str) -> str:
    if key in _KEY_GROUP:
        return _KEY_GROUP[key]
    if key.replace("_", " ").lower() in _RUNNING_DYNAMICS:
        return "Running dynamics"
    return "Other"


# Swimming: pace is per 100 m (not min/km) and "Cadence" is strokes/min.
_SWIM_LABELS = {"pace": "Pace (min/100m)", "cadence": "Strokes/min"}


def chart_options(keys, sport: str | None = None) -> list[dict]:
    """[{key,label,group}] of the present plot channels, grouped + sorted for
    the line selection boxes. Fixed order within known groups,
    "Running dynamics"/"Other" alphabetically by label. `sport` adjusts labels
    (swimming: Pace /100m, Strokes/min)."""
    meta = {m["key"]: m["label"] for m in series_meta(keys)}
    if sport == "swimming":
        meta.update({k: v for k, v in _SWIM_LABELS.items() if k in meta})
    order_in_group = {k: i for _t, ks in _CHART_GROUPS for i, k in enumerate(ks)}
    items = [{"key": k, "label": meta[k], "group": _chart_group(k)} for k in keys]

    def sort_key(it):
        g = _GROUP_ORDER.index(it["group"])
        within = order_in_group.get(it["key"], 10_000)
        return (g, within, it["label"].lower())

    return sorted(items, key=sort_key)


def _coord(v):
    if not isinstance(v, (int, float)):
        return None
    d = v * _SEMI
    return d if -180.0 <= d <= 180.0 else None


def _ffill(seq: list) -> None:
    """Forward fill in place: None -> last known value (leading Nones
    stay None). For sparse slow-sensor channels."""
    last = None
    for i, x in enumerate(seq):
        if x is not None:
            last = x
        elif last is not None:
            seq[i] = last


def _num(rec: dict, names, factor: float):
    for n in names:
        v = rec.get(n)
        if isinstance(v, (int, float)):
            return round(v * factor, 3)
    return None


def _read_records(data: bytes, lengths: list[dict] | None = None,
                  ) -> tuple[list[dict], list[dict], list[dict]]:
    """Reads record, session AND lap messages in one pass; if a list `lengths`
    is passed, the length messages (pool lengths) end up in it."""
    recs: list[dict] = []
    sess: list[dict] = []
    laps: list[dict] = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with fitdecode.FitReader(io.BytesIO(data)) as fr:
            for frame in fr:
                if not isinstance(frame, fitdecode.FitDataMessage):
                    continue
                if frame.name == "record":
                    recs.append({f.name: f.value for f in frame.fields})
                elif frame.name == "session":
                    sess.append({f.name: f.value for f in frame.fields})
                elif frame.name == "lap":
                    laps.append({f.name: f.value for f in frame.fields})
                elif frame.name == "length" and lengths is not None:
                    lengths.append({f.name: f.value for f in frame.fields})
    return recs, sess, laps


def _merge_same_ts(recs: list[dict]) -> list[dict]:
    """Merge records with an identical timestamp into ONE.

    The COROS PACE Pro writes TWO record messages per second: one with the
    Stryd CIQ fields (Air Power, Form Power, LSS), one with the native
    fields (GPS, speed, power, altitude). The index-stride downsampling then
    hit almost only the CIQ records - Stadtlauf Erding 2025-09-21: 2 of 2885
    GPS points, altitude/distance 1 value each, chart without speed/power. Only for
    the display streams; byte patchers (hr_csv) still need ONE entry per
    message."""
    out: list[dict] = []
    for r in recs:
        if out and out[-1].get("timestamp") == r.get("timestamp"):
            out[-1].update({k: v for k, v in r.items() if v is not None})
        else:
            out.append(dict(r))
    return out


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance of two points (decimal degrees) in meters."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _gps_distance_from_records(recs: list[dict]) -> float | None:
    """Haversine sum over consecutive valid GPS points (meters).
    None for <2 positions (no usable GPS track)."""
    total = 0.0
    prev = None
    n = 0
    for r in recs:
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is None or lo is None or (la == 0 and lo == 0):
            continue
        if prev is not None:
            total += _haversine_m(prev[0], prev[1], la, lo)
        prev = (la, lo)
        n += 1
    return round(total, 1) if n >= 2 else None


def gps_track_distance(data: bytes) -> float | None:
    """Route length from the FIT's GPS points (meters); None without a GPS track.
    For footpod runs this differs from the recorded total_distance."""
    recs, _, _ = _read_records(data)
    return _gps_distance_from_records(recs)


def _session_legs(sess: list[dict], t0) -> list[dict]:
    """Multisport legs as selection metadata: label + time window (second
    offsets matching the t axis) + metrics from the session message.
    Transitions are named T1/T2…; repeated sports are numbered
    (duathlon: Run 1/Run 2). Only filled with >1 session."""
    if len(sess) < 2 or t0 is None:
        return []
    legs: list[dict] = []
    for d in sess:
        start = d.get("start_time")
        elapsed = d.get("total_elapsed_time")
        if not isinstance(elapsed, (int, float)):
            elapsed = d.get("total_timer_time")
        if not (isinstance(start, datetime) and isinstance(elapsed, (int, float))):
            continue
        sport = _enum_name(d.get("sport"), _SPORT_NAMES) or "generic"
        speed = d.get("enhanced_avg_speed")
        if not isinstance(speed, (int, float)):
            speed = d.get("avg_speed")
        legs.append({
            "sport": sport,
            "sub_sport": _enum_name(d.get("sub_sport"), _SUB_SPORT_NAMES),
            "start_s": round((start - t0).total_seconds(), 1),
            "end_s": round((start - t0).total_seconds() + elapsed, 1),
            "dist_m": d.get("total_distance")
                      if isinstance(d.get("total_distance"), (int, float)) else None,
            "dur_s": round(d.get("total_timer_time") or elapsed),
            "avg_hr": d.get("avg_heart_rate")
                      if isinstance(d.get("avg_heart_rate"), (int, float)) else None,
            "avg_power": d.get("avg_power")
                         if isinstance(d.get("avg_power"), (int, float)) else None,
            "norm_power": d.get("normalized_power")
                          if isinstance(d.get("normalized_power"), (int, float)) else None,
            "avg_speed_ms": speed if isinstance(speed, (int, float)) else None,
        })
    if len(legs) < 2:
        return []
    # Labels: transitions as T1/T2…, sport legs numbered if needed
    base = [(_LEG_LABELS.get(l["sport"], l["sport"].capitalize())
             if l["sport"] != "transition" else "T") for l in legs]
    counts = {b: base.count(b) for b in base}
    seen: dict[str, int] = {}
    for leg, b in zip(legs, base):
        seen[b] = seen.get(b, 0) + 1
        if b == "T":
            leg["label"] = f"T{seen[b]}"
        else:
            leg["label"] = f"{b} {seen[b]}" if counts[b] > 1 else b
    return legs


def _laps(laps: list[dict], t0, names: dict | None = None) -> list[dict]:
    """Laps (lap messages) as display/zoom metadata: time window (second
    offsets matching the t axis) + metrics straight from the lap message.
    `names` (sidecar, {1-based-index-str: label}) overrides the default name.
    Empty if <2 usable laps (one lap = the whole activity, uninteresting)."""
    if not laps or t0 is None:
        return []
    names = names or {}
    out: list[dict] = []
    for i, d in enumerate(laps):
        elapsed = d.get("total_elapsed_time")
        if not isinstance(elapsed, (int, float)):
            elapsed = d.get("total_timer_time")
        start = d.get("start_time")
        if not isinstance(start, datetime):
            end = d.get("timestamp")               # lap.timestamp = end time
            if isinstance(end, datetime) and isinstance(elapsed, (int, float)):
                start = end - timedelta(seconds=elapsed)
        if not (isinstance(start, datetime) and isinstance(elapsed, (int, float))):
            continue
        # First lap can lie slightly before t0 due to rounding/start offset -> clamp to 0.
        start_s = max(0.0, round((start - t0).total_seconds(), 1)) if i == 0 \
            else round((start - t0).total_seconds(), 1)
        speed = d.get("enhanced_avg_speed")
        if not isinstance(speed, (int, float)):
            speed = d.get("avg_speed")

        def _n(*keys):
            for k in keys:
                v = d.get(k)
                if isinstance(v, (int, float)):
                    return v
            return None

        dist_m = _n("total_distance")
        dur_s = round(d.get("total_timer_time") or elapsed)
        # Some devices lack avg_speed in the lap message -> derive from distance/time
        # (average pace of the lap, good enough for the table display).
        if not isinstance(speed, (int, float)) and dist_m and dur_s:
            speed = dist_m / dur_s
        idx = i + 1
        out.append({
            "idx": idx,
            "name": names.get(str(idx)),
            "start_s": start_s,
            "end_s": round(start_s + elapsed, 1),
            "dist_m": dist_m,
            "dur_s": dur_s,
            "avg_hr": _n("avg_heart_rate"),
            "max_hr": _n("max_heart_rate"),
            "avg_power": _n("avg_power"),
            "max_power": _n("max_power"),
            "avg_cadence": _n("avg_cadence", "avg_running_cadence"),
            "avg_speed_ms": speed if isinstance(speed, (int, float)) else None,
        })
    return out if len(out) > 1 else []


_TRACK_MAX_POINTS = 400         # heatmap resolution per activity (compact)


def extract_track(data: bytes) -> dict:
    """GPS-only, downsampled route of an activity for the heatmap:
      {"track": [[lat,lng], …], "bounds": [[minlat,minlng],[maxlat,maxlng]]|None}.
    Empty/GPS-less file -> {"track": [], "bounds": None}. Much cheaper
    than extract_streams (no channels/sessions, only positions)."""
    out: dict = {"track": [], "bounds": None}
    try:
        recs, _, _ = _read_records(data)
    except Exception:  # noqa: BLE001 — broken file -> empty
        return out
    pts: list[tuple[float, float]] = []
    for r in recs:
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is not None and lo is not None and not (la == 0 and lo == 0):
            pts.append((la, lo))
    if not pts:
        return out
    stride = max(1, -(-len(pts) // _TRACK_MAX_POINTS))    # ceil -> <= _TRACK_MAX_POINTS
    track = [[round(la, 5), round(lo, 5)]
             for i, (la, lo) in enumerate(pts) if i % stride == 0 or i == len(pts) - 1]
    lats = [p[0] for p in track]
    lngs = [p[1] for p in track]
    out["track"] = track
    out["bounds"] = [[min(lats), min(lngs)], [max(lats), max(lngs)]]
    return out


def _mean(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else None


def _rec_field(r: dict, *keys):
    for k in keys:
        v = r.get(k)
        if isinstance(v, (int, float)):
            return v
    return None


def _dist_at(dpts: list[tuple[float, float]], sec: float):
    """Distance at second `sec`, linearly interpolated between the records
    (before/after: first/last value). None without a distance series."""
    if not dpts:
        return None
    i = bisect.bisect_left(dpts, (sec, float("-inf")))
    if i == 0:
        return dpts[0][1]
    if i >= len(dpts):
        return dpts[-1][1]
    (ta, da), (tb, db) = dpts[i - 1], dpts[i]
    return da if tb <= ta else da + (db - da) * (sec - ta) / (tb - ta)


def _lap_window_stats(grp: list[dict], s0: float, s1: float,
                      dpts: list[tuple[float, float]] | None = None) -> dict:
    """Display metrics of a lap from the records in the window [s0, s1).
    The distance is interpolated at the boundaries (`dpts` = distance series of
    the whole activity): with smart recording the records are up to 6 s
    apart, and "first to last record in the window" made every
    kilometer split come out 5-30 m too short - so with the exact time, too
    slow (6:07 instead of 6:00 /km)."""
    hr = [r.get("heart_rate") for r in grp]
    pw = [r.get("power") for r in grp]
    cad = [_rec_field(r, "cadence", "running_cadence") for r in grp]
    spd = [_rec_field(r, "enhanced_speed", "speed") for r in grp]
    if dpts:
        dist = _dist_at(dpts, s1) - _dist_at(dpts, s0)
    else:
        dists = [r.get("distance") for r in grp if isinstance(r.get("distance"), (int, float))]
        dist = (dists[-1] - dists[0]) if len(dists) >= 2 else None
    if dist is not None and dist < 0:
        dist = None
    dur = max(0.0, s1 - s0)
    avg_speed = (dist / dur) if (dist and dur > 0) else _mean(spd)
    hrv = [h for h in hr if isinstance(h, (int, float))]
    pwv = [p for p in pw if isinstance(p, (int, float))]
    mhr, mpw, mcad = _mean(hr), _mean(pw), _mean(cad)
    return {
        "dist_m": round(dist, 1) if dist is not None else None,
        "dur_s": round(dur),
        "avg_hr": round(mhr) if mhr is not None else None,
        "max_hr": max(hrv) if hrv else None,
        "avg_power": round(mpw) if mpw is not None else None,
        "max_power": max(pwv) if pwv else None,
        "avg_cadence": round(mcad) if mcad is not None else None,
        "avg_speed_ms": round(avg_speed, 3) if avg_speed is not None else None,
    }


def _laps_from_bounds(recs: list[dict], t0, bounds_s: list[float],
                      names: dict | None = None) -> list[dict]:
    """Laps from edited boundaries `bounds_s` (ascending second offsets incl.
    0 and end time): recompute the metrics from the records per window. Empty
    if <2 boundaries (= <1 lap). The correction FIT build uses the same computation."""
    names = names or {}
    if t0 is None or not bounds_s or len(bounds_s) < 2:
        return []
    pts = []
    dpts: list[tuple[float, float]] = []
    for r in recs:
        ts = r.get("timestamp")
        if isinstance(ts, datetime):
            sec = round((ts - t0).total_seconds(), 3)
            pts.append((sec, r))
            d = r.get("distance")
            if isinstance(d, (int, float)):
                dpts.append((sec, float(d)))
    out = []
    n = len(bounds_s) - 1
    for i in range(n):
        s0, s1 = bounds_s[i], bounds_s[i + 1]
        last = (i == n - 1)
        grp = [r for (sec, r) in pts
               if s0 <= sec < s1 or (last and sec == s1)]
        st = _lap_window_stats(grp, s0, s1, dpts)
        st.update({"idx": i + 1, "name": names.get(str(i + 1)),
                   "start_s": round(s0, 1), "end_s": round(s1, 1)})
        out.append(st)
    return out


def lap_bounds_native(data: bytes) -> list[float]:
    """Native lap boundaries of the FIT as second offsets [0, …, end time] — start value
    for the editor / "back to original". Empty if <2 laps."""
    try:
        recs, _, laps = _read_records(data)
    except Exception:  # noqa: BLE001
        return []
    t0 = next((r["timestamp"] for r in recs if isinstance(r.get("timestamp"), datetime)), None)
    lp = _laps(laps, t0)
    if not lp:
        return []
    return [lp[0]["start_s"]] + [l["end_s"] for l in lp]


def extract_streams(data: bytes, lap_names: dict | None = None,
                    lap_bounds: list[float] | None = None) -> dict:
    """Returns a dict:
      n        – number of sampled points
      t        – x axis: elapsed seconds since start
      channels – {key: [values|None]} only for channels with real data
                 (channels containing only None or only 0 are left out)
      gps      – [[lat,lng], …] (simplified route; may be empty)
      lat/lng  – sample-aligned coordinates (length == n, None without a fix);
                 for the chart cursor -> map marker. Empty without GPS.
      sessions – multisport legs ({label, sport, start_s, end_s, dist_m,
                 dur_s, avg_hr, avg_power, avg_speed_ms}); empty with <2 sessions
      splits   – auto splits (1 km running / 5 km cycling / 250 m open water) in the
                 lap schema; empty for multisport, pool swimming, <1 split
      splits_rest – {dist_m, dur_s} of a rest < 100 m after the last
                 full split (not a split of its own; the table mentions it), else None
      best     – {"power": {peaks, curve} | None, "dist": [{m, label, s}]}
                 from the full-resolution records (best_efforts; pool swimming
                 from the length messages); for multisport
                 instead "legs": [{idx, label, sport, sub_sport, power, dist}]
                 per sport leg (idx = 1-based position in `sessions`)
      bounds   – [[minlat,minlng],[maxlat,maxlng]] or None
    """
    empty = {"n": 0, "t": [], "channels": {}, "gps": [], "lat": [], "lng": [],
             "maxima": {}, "sessions": [], "laps": [], "splits": [], "splits_rest": None,
             "best": {"power": None, "dist": []}, "bounds": None, "te": None}
    lengths: list[dict] = []
    try:
        recs, sess, laps = _read_records(data, lengths)
    except Exception:  # noqa: BLE001 — broken file -> empty result
        return empty
    if not recs:
        return empty
    recs = _merge_same_ts(recs)

    t0 = next((r["timestamp"] for r in recs if isinstance(r.get("timestamp"), datetime)), None)
    n = len(recs)
    stride = max(1, -(-n // _MAX_POINTS))    # ceil -> result stays <= _MAX_POINTS

    plan = _build_plan(recs)                 # auto-detect this file's channels
    t: list[float] = []
    cols: dict[str, list] = {key: [] for key, _, _ in plan}
    gps: list[list[float]] = []
    lat_arr: list = []
    lng_arr: list = []
    lat_min = lat_max = lng_min = lng_max = None

    for i, r in enumerate(recs):
        if i % stride != 0 and i != n - 1:
            continue
        ts = r.get("timestamp")
        t.append(round((ts - t0).total_seconds(), 1) if (isinstance(ts, datetime) and t0) else None)
        for key, names, factor in plan:
            cols[key].append(_num(r, names, factor))
        la, lo = _coord(r.get("position_lat")), _coord(r.get("position_long"))
        if la is not None and lo is not None and not (la == 0 and lo == 0):
            gps.append([round(la, 5), round(lo, 5)])
            lat_arr.append(round(la, 5))
            lng_arr.append(round(lo, 5))
            lat_min = la if lat_min is None else min(lat_min, la)
            lat_max = la if lat_max is None else max(lat_max, la)
            lng_min = lo if lng_min is None else min(lng_min, lo)
            lng_max = lo if lng_max is None else max(lng_max, lo)
        else:
            lat_arr.append(None)
            lng_arr.append(None)

    # Maxima over ALL records (not the downsampled cols) — otherwise the
    # peak value (e.g. max speed) would be swallowed by the stride.
    cmax: dict[str, float] = {}
    for r in recs:
        for key, names, factor in plan:
            v = _num(r, names, factor)
            if v is not None and (key not in cmax or v > cmax[key]):
                cmax[key] = v

    # keep only channels that carry real data (at least one value != None/0 —
    # a stream that is empty or 0 throughout is not a displayable data stream)
    channels = {k: v for k, v in cols.items()
                if any(x is not None and x != 0 for x in v)}
    # Forward-fill sparse channels (slow sensors like core/skin temp that only send
    # every few seconds): otherwise after downsampling almost only None is
    # left -> no visible line with spanGaps:false. Dense channels with
    # real dropouts (HR/power) stay untouched (threshold 90 %).
    for v in channels.values():
        nn = sum(1 for x in v if x is not None)
        if 0 < nn < 0.9 * len(v):
            _ffill(v)
    bounds = ([[lat_min, lng_min], [lat_max, lng_max]] if lat_min is not None else None)
    if not gps:
        lat_arr, lng_arr = [], []
    maxima = {k: cmax[k] for k in channels if k in cmax}
    # Edited boundaries (sidecar) take precedence -> recompute metrics from the
    # records; otherwise the native lap messages (Garmin values).
    lap_list = (_laps_from_bounds(recs, t0, lap_bounds, lap_names) if lap_bounds
                else _laps(laps, t0, lap_names))
    legs = _session_legs(sess, t0)
    # Best efforts + auto splits: single sport only (across multisport legs
    # peaks/splits would be nonsense), no splits for pool swimming (length editor).
    sport = sub = None
    if sess and not legs:
        sport = _enum_name(sess[0].get("sport"), _SPORT_NAMES)
        sub = _enum_name(sess[0].get("sub_sport"), _SUB_SPORT_NAMES)
    splits: list[dict] = []
    splits_rest = None
    best: dict = {"power": None, "dist": []}
    if t0 is not None and sport is not None:
        best = {"power": best_efforts.power_peaks(recs, t0),
                "dist": (best_efforts.fastest_pool_distances(lengths, sess[0].get("pool_length"))
                         if sub == "lap_swimming"
                         else best_efforts.fastest_distances(recs, t0, sport))}
        if sub != "lap_swimming" and t and t[-1] is not None:
            sb, names, _step, rest = best_efforts.split_bounds(recs, t0, sport, t[-1])
            if len(sb) >= 2:
                splits = _laps_from_bounds(recs, t0, sb, names)
                splits_rest = rest
    elif t0 is not None and legs:
        # Multisport: best efforts per sport leg from its record window
        # (transitions are left out); each leg is ranked in its own sport.
        best["legs"] = []
        for i, leg in enumerate(legs, 1):
            if leg["sport"] == "transition":
                continue
            lo, hi = leg["start_s"], leg["end_s"]
            lrecs = [r for r in recs
                     if isinstance(r.get("timestamp"), datetime)
                     and lo <= (r["timestamp"] - t0).total_seconds() <= hi]
            lp = best_efforts.power_peaks(lrecs, t0)
            ld = best_efforts.fastest_distances(lrecs, t0, leg["sport"])
            if lp or ld:
                best["legs"].append({"idx": i, "label": leg["label"], "sport": leg["sport"],
                                     "sub_sport": leg.get("sub_sport"), "power": lp, "dist": ld})
    return {"n": len(t), "t": t, "channels": channels, "gps": gps,
            "lat": lat_arr, "lng": lng_arr, "maxima": maxima,
            "sessions": legs, "laps": lap_list, "splits": splits, "splits_rest": splits_rest,
            "best": best, "bounds": bounds}


def extract_hr(data: bytes) -> dict:
    """Full-resolution HR series for the HR editor: ONE entry per record message
    (gnum 20), in file order — the index matches exactly the byte writer
    in hr_edit.apply_hr_edits (NO downsampling, unlike extract_streams).
      t   – seconds since start (or None)
      hr  – bpm or None (no/invalid value)
    {n, t, hr, has_hr, t0_epoch} — t0_epoch = UTC epoch of the first record
    (t + t0_epoch = absolute record time, e.g. for the CSV mapping)."""
    empty = {"n": 0, "t": [], "hr": [], "has_hr": False, "t0_epoch": None}
    try:
        recs, _, _ = _read_records(data)
    except Exception:        # noqa: BLE001 — broken file -> empty result
        return empty
    if not recs:
        return empty
    t0 = next((r["timestamp"] for r in recs
               if isinstance(r.get("timestamp"), datetime)), None)
    t: list = []
    hr: list = []
    for r in recs:
        ts = r.get("timestamp")
        t.append(round((ts - t0).total_seconds(), 1)
                 if (isinstance(ts, datetime) and t0) else None)
        v = r.get("heart_rate")
        hr.append(int(v) if isinstance(v, (int, float)) and v > 0 else None)
    return {"n": len(recs), "t": t, "hr": hr,
            "has_hr": any(x is not None for x in hr),
            "t0_epoch": t0.timestamp() if t0 else None}


def _secs(d: dict):
    v = d.get("total_timer_time")
    if not isinstance(v, (int, float)):
        v = d.get("total_elapsed_time")
    return round(v, 1) if isinstance(v, (int, float)) else None


def extract_swim(data: bytes) -> dict:
    """Pool swimming for the pace chart + interval table:
      pool_m  – pool length (m)
      lengths – per length: {active, stroke, sec, pace100 (s/100m, active only), hr}
      laps    – active intervals: {dist, lengths, sec, pace100, stroke, hr, spl}
                (hr = avg heart rate, spl = avg strokes per length)
    """
    out = {"pool_m": None, "lengths": [], "laps": []}
    windows: list = []          # (start, end) per length, for the per-length HR
    recs: list = []             # (timestamp, heart_rate)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for frame in fr:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    if frame.name == "session":
                        v = {f.name: f.value for f in frame.fields}.get("pool_length")
                        if isinstance(v, (int, float)) and v > 0:
                            out["pool_m"] = float(v)
                    elif frame.name == "record":
                        d = {f.name: f.value for f in frame.fields}
                        ts, hr = d.get("timestamp"), d.get("heart_rate")
                        if isinstance(ts, datetime) and isinstance(hr, (int, float)):
                            recs.append((ts, int(hr)))
                    elif frame.name == "length":
                        d = {f.name: f.value for f in frame.fields}
                        stroke = d.get("swim_stroke")
                        out["lengths"].append({
                            "active": str(d.get("length_type") or "active") == "active",
                            "stroke": str(stroke) if stroke else None,
                            "sec": _secs(d),
                        })
                        start = d.get("start_time")
                        elapsed = d.get("total_elapsed_time")
                        if not isinstance(elapsed, (int, float)):
                            elapsed = d.get("total_timer_time")
                        windows.append((start, start + timedelta(seconds=elapsed))
                                       if isinstance(start, datetime)
                                       and isinstance(elapsed, (int, float)) else None)
                    elif frame.name == "lap":
                        d = {f.name: f.value for f in frame.fields}
                        dist, sec = d.get("total_distance"), _secs(d)
                        if isinstance(dist, (int, float)) and dist > 0 and sec:
                            st = d.get("swim_stroke")
                            n = int(d.get("num_active_lengths") or 0)
                            strokes = d.get("total_strokes")        # swim alias …
                            if not isinstance(strokes, (int, float)):
                                strokes = d.get("total_cycles")     # … or base field
                            hr = d.get("avg_heart_rate")
                            out["laps"].append({
                                "dist": round(dist),
                                "lengths": n,
                                "sec": sec,
                                "pace100": round(sec * 100.0 / dist, 1),
                                "stroke": str(st) if st else None,
                                "hr": int(hr) if isinstance(hr, (int, float)) else None,
                                "spl": (round(strokes / n)
                                        if isinstance(strokes, (int, float)) and n else None),
                            })
    except Exception:  # noqa: BLE001
        return {"pool_m": None, "lengths": [], "laps": []}

    pool = out["pool_m"]
    for L, w in zip(out["lengths"], windows):
        L["pace100"] = (round(L["sec"] * 100.0 / pool, 1)
                        if (L["active"] and pool and L["sec"]) else None)
        L["hr"] = None
        if w and recs:
            vals = [h for (t, h) in recs if w[0] <= t <= w[1]]
            if vals:
                L["hr"] = round(sum(vals) / len(vals))
    return out


def _swim_lap_assignment(laps: list[dict], lengths_raw: list[dict],
                         n: int) -> list[int]:
    """0-based interval index per length (for the length editor): prefers the
    index ranges of the lap messages (first_length_index/num_lengths), otherwise the
    time windows of the lap starts, otherwise everything in lap 0. Best effort — only
    serves the grouping/divider lines, not the correctness of the totals."""
    lap_of = [0] * n
    if n == 0:
        return lap_of
    ranges, ok = [], True
    for lp in laps:
        fli, nl = lp.get("first_length_index"), lp.get("num_lengths")
        if isinstance(fli, int) and isinstance(nl, int) and nl > 0:
            ranges.append((fli, nl))
        else:
            ok = False
            break
    if ok and ranges:
        for li, (fli, nl) in enumerate(ranges):
            for idx in range(fli, min(fli + nl, n)):
                if 0 <= idx < n:
                    lap_of[idx] = li
        return lap_of
    starts = sorted(s for lp in laps
                    if isinstance((s := lp.get("start_time")), datetime))
    if starts and all(isinstance(lengths_raw[i].get("start_time"), datetime)
                      for i in range(n)):
        for i in range(n):
            ls = lengths_raw[i]["start_time"]
            li = 0
            for k, s in enumerate(starts):
                if s <= ls:
                    li = k
                else:
                    break
            lap_of[i] = li
    return lap_of
