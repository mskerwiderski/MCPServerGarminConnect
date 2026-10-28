"""FIT file -> metrics. Uses fitdecode (robust against the malformed
intervals.icu fields that fitparse would flatly reject).

All metrics come from the first `session` message; if a field is missing,
it stays None. Across the sources (Garmin, Apple HealthKit, Coros, Zwift,
FORM, WorkOutDoors, intervals.icu) the session field names are consistent;
the variants (running_cadence vs. cadence, enhanced_* speed,
missing total_ascent) are normalized here.
"""

from __future__ import annotations

import io
import warnings
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from datetime import datetime
from typing import Optional

import fitdecode


class FitParseError(Exception):
    """File is not readable as FIT."""


@dataclass
class FitMetrics:
    sport: Optional[str] = None
    sub_sport: Optional[str] = None
    start_time: Optional[datetime] = None
    duration_s: Optional[int] = None       # total_moving_time (moving / net, = Strava)
    elapsed_s: Optional[int] = None        # total_elapsed_time (total / gross)
    distance_m: Optional[float] = None
    calories: Optional[int] = None
    avg_hr: Optional[int] = None
    max_hr: Optional[int] = None
    avg_power: Optional[int] = None
    max_power: Optional[int] = None
    norm_power: Optional[int] = None
    avg_speed_ms: Optional[float] = None
    total_ascent_m: Optional[int] = None
    total_descent_m: Optional[int] = None
    avg_cadence: Optional[int] = None
    total_strides: Optional[int] = None    # running/walking: strides (steps = 2 ×)
    tss: Optional[float] = None
    device: Optional[str] = None
    serial_number: Optional[int] = None
    manufacturer: Optional[str] = None
    product: Optional[str] = None        # model/product
    software_version: Optional[str] = None
    # Producing CIQ app from the developer_data_id UUID (e.g. "cellTrainer
    # 1.2.3 (45)"). Reliable fingerprint of our own app, even when it poses as a
    # fake Garmin device (FR310XT) via file_id. None for third-party sources.
    produced_by: Optional[str] = None
    title: Optional[str] = None          # workout.wkt_name
    description: Optional[str] = None     # session.description
    start_lat: Optional[float] = None    # degrees (from semicircles)
    start_lng: Optional[float] = None

    def as_dict(self) -> dict:
        return asdict(self)


# Canonical list of the derived metric fields (for re-parse/reset).
METRIC_FIELDS: tuple[str, ...] = tuple(f.name for f in dataclass_fields(FitMetrics))


def _as_int(v) -> Optional[int]:
    return int(v) if isinstance(v, (int, float)) else None


def _as_float(v) -> Optional[float]:
    return float(v) if isinstance(v, (int, float)) else None


def _as_str(v) -> Optional[str]:
    if v is None:
        return None
    return str(v)


# Garmin sport/sub_sport names — fallback for values that fitdecode's (older)
# profile returns as numbers (e.g. brick=80).
_SPORT_NAMES = {
    0: "generic", 1: "running", 2: "cycling", 3: "transition",
    4: "fitness_equipment", 5: "swimming", 8: "tennis", 10: "training",
    11: "walking", 17: "hiking", 18: "multisport", 64: "racket",
}
_SUB_SPORT_NAMES = {
    0: "generic", 17: "lap_swimming", 18: "open_water",
    19: "flexibility_training", 20: "strength_training", 22: "match",
    26: "cardio_training", 32: "bike_to_run_transition",
    33: "run_to_bike_transition", 34: "swim_to_bike_transition",
    78: "triathlon", 79: "duathlon", 80: "brick", 81: "swim_run",
    95: "badminton", 97: "table_tennis",
}


def _enum_name(v, table: dict) -> Optional[str]:
    """Keeps names resolved by fitdecode; purely numeric values are mapped to
    the Garmin name (otherwise kept as a number string)."""
    if v is None:
        return None
    if isinstance(v, str) and not v.isdigit():
        return v
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return str(v)
    return table.get(iv, str(iv))


def _clean_text(v) -> Optional[str]:
    """Clean free text: normalize non-breaking spaces, trim,
    empty strings -> None."""
    if v is None:
        return None
    t = str(v).replace("\xa0", " ").strip()
    return t or None


def _power_fallback(session_avg, session_max, rp: dict):
    """Returns (avg_power, max_power). Prefers the session values; if they are
    missing, they are computed from the record.power aggregate (rp) — many running
    power sources (Coros/Stryd) fill only the records, not the session total."""
    avg = session_avg
    if avg is None and rp["count"]:
        avg = round(rp["sum"] / rp["count"])
    mx = session_max
    if mx is None and rp["max"] is not None:
        mx = int(rp["max"])
    return avg, mx


# Gap between two records up to which the last value keeps applying (smart
# recording writes every few seconds); a longer one is a pause and
# counts as just one second.
_NP_MAX_GAP_S = 10


def normalized_power(xs: list) -> float | None:
    """Normalized Power (Coggan approximation) of a power series with 1 value per
    second: 30 s rolling mean, then the fourth root of the mean of the
    fourth powers. None if empty."""
    if not xs:
        return None
    w = 30
    if len(xs) < w:
        roll = [sum(xs) / len(xs)]
    else:
        roll, s = [], 0.0
        for i, v in enumerate(xs):
            s += v
            if i >= w:
                s -= xs[i - w]
            if i >= w - 1:
                roll.append(s / w)
    m4 = sum(p ** 4 for p in roll) / len(roll)
    return m4 ** 0.25


def _np_fallback(session_np, samples: list) -> Optional[int]:
    """NP of the session, otherwise from the records ([(unix_s | None, watts), …]).

    Zwift, COROS, cellTrainer, icTrainer write no `normalized_power` into
    the session - TSS estimate and IF then used the average power. For
    intervals that is badly wrong: HIT 2x(4x4min) on 2026-10-14 had avg 156 W,
    NP 190 W (intervals.icu: 190) -> IF 0.68 instead of 0.83, TSS 94 instead of 139.
    The series is first brought to 1 value per second, otherwise the
    30 s window would be several minutes long with smart recording."""
    if session_np is not None or not samples:
        return session_np
    grid = []
    for (t, p), (t_next, _) in zip(samples, samples[1:] + [(None, None)]):
        dt = t_next - t if t is not None and t_next is not None else 1
        if dt <= 0:                          # same timestamp: the next one applies
            continue
        grid.extend([p] * (round(dt) if dt <= _NP_MAX_GAP_S else 1))
    np_ = normalized_power(grid)
    return round(np_) if np_ is not None else None


def _coord(v, limit: float) -> Optional[float]:
    """FIT semicircles -> decimal degrees; invalid/empty values -> None."""
    if not isinstance(v, (int, float)):
        return None
    deg = v * (180.0 / 2**31)
    return deg if -limit <= deg <= limit else None


_RECORD_GPS_SCAN_LIMIT = 120  # scan this many records for the first GPS fix


def _first_messages(data: bytes) -> dict:
    """Collects the messages relevant for the mapping. Raises FitParseError
    if the file cannot be read at all."""
    out = {"file_id": {}, "session": {}, "sessions": [], "workout": {},
           "first_gps": {}, "file_creator": {}, "device_info": {},
           # ONLY the device_info with device_index=='creator' (= recording device);
           # NO fallback to the first one (that would often be a sensor: hrm_pro/gnss).
           "device_creator": {},
           # developer_data_id messages (per CIQ app application_id UUID + version)
           # -> producer detection (cellTrainer fingerprint).
           "dev_apps": [],
           # Aggregate from record.power — fallback when the session carries no
           # power total (e.g. Coros/Stryd running power).
           "record_power": {"sum": 0.0, "count": 0, "max": None, "samples": []},
           # Sum of the active length time (length messages) — that is the real
           # swim moving time (= Strava "Moving"), even when the session carries no
           # total_moving_time (applies to older pool swims).
           "swim_active_s": {"sum": 0.0, "n": 0}}
    rec_scanned = 0
    seen_device_info = False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with fitdecode.FitReader(io.BytesIO(data)) as fr:
                for frame in fr:
                    if not isinstance(frame, fitdecode.FitDataMessage):
                        continue
                    n = frame.name
                    if n == "file_id" and not out["file_id"]:
                        out["file_id"] = {f.name: f.value for f in frame.fields}
                    elif n == "session":
                        sd = {f.name: f.value for f in frame.fields}
                        out["sessions"].append(sd)
                        if not out["session"]:
                            out["session"] = sd
                    elif n == "length":
                        d = {f.name: f.value for f in frame.fields}
                        if str(d.get("length_type") or "active") == "active":
                            sec = d.get("total_timer_time")
                            if not isinstance(sec, (int, float)):
                                sec = d.get("total_elapsed_time")
                            if isinstance(sec, (int, float)):
                                out["swim_active_s"]["sum"] += sec
                                out["swim_active_s"]["n"] += 1
                    elif n == "workout" and not out["workout"]:
                        out["workout"] = {f.name: f.value for f in frame.fields}
                    elif n == "file_creator" and not out["file_creator"]:
                        out["file_creator"] = {f.name: f.value for f in frame.fields}
                    elif n == "developer_data_id":
                        out["dev_apps"].append({f.name: f.value for f in frame.fields})
                    elif n == "device_info":
                        d = {f.name: f.value for f in frame.fields}
                        # prefer the "creator" device (= recording device)
                        if d.get("device_index") == "creator":
                            out["device_info"] = d
                            out["device_creator"] = d
                        elif not seen_device_info:
                            out["device_info"] = d
                            seen_device_info = True
                    elif n == "record":
                        # One pass over the fields: power (always
                        # accumulate) + first GPS fix (only within the scan window).
                        need_gps = (
                            not out["first_gps"]
                            and rec_scanned < _RECORD_GPS_SCAN_LIMIT
                        )
                        pwr = lat = lng = ts = None
                        for fld in frame.fields:
                            if fld.name == "power":
                                pwr = fld.value
                            elif fld.name == "timestamp":
                                ts = fld.value
                            elif need_gps and fld.name == "position_lat":
                                lat = fld.value
                            elif need_gps and fld.name == "position_long":
                                lng = fld.value
                        if isinstance(pwr, (int, float)):
                            rp = out["record_power"]
                            rp["sum"] += pwr
                            rp["count"] += 1
                            if rp["max"] is None or pwr > rp["max"]:
                                rp["max"] = pwr
                            rp["samples"].append(
                                (ts.timestamp() if isinstance(ts, datetime) else None, pwr))
                        if need_gps:
                            rec_scanned += 1
                            if lat is not None and lng is not None:
                                out["first_gps"] = {"lat": lat, "long": lng}
    except Exception as exc:  # fitdecode raises various error types
        raise FitParseError(str(exc)) from exc
    return out


def _pick_product(d: dict):
    """Model field from a message (file_id/device_info). Garmin calls it
    `garmin_product` (resolved to names by fitdecode, e.g. fenix8/edge_820)."""
    return (_clean_text(d.get("product_name")) or _clean_text(d.get("garmin_product"))
            or _clean_text(d.get("product")))


def _model_product(file_id: dict, dev: dict):
    """Model of the RECORDING device. device_info(creator) takes precedence over file_id
    if the manufacturer matches — early Garmin firmware (e.g. Fenix 3, 2015/16)
    wrongly wrote fr920xt (1765) into file_id, while device_info correctly reports the
    device (fenix3)."""
    fid_m = _clean_text(file_id.get("manufacturer"))
    dev_m = _clean_text((dev or {}).get("manufacturer"))
    if dev and dev_m and dev_m == fid_m:
        return _pick_product(dev) or _pick_product(file_id)
    return _pick_product(file_id) or (_pick_product(dev) if not fid_m else None)


def _device_str(manufacturer, product) -> Optional[str]:
    parts = [str(p) for p in (manufacturer, product) if p not in (None, "", 0)]
    return " / ".join(parts) or None


def _session_dur(se: dict):
    d = se.get("total_moving_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_timer_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_elapsed_time")
    return d if isinstance(d, (int, float)) else None


def _session_elapsed(se: dict):
    d = se.get("total_elapsed_time")
    if not isinstance(d, (int, float)):
        d = se.get("total_timer_time")
    return d if isinstance(d, (int, float)) else None


def _session_speed(se: dict):
    v = se.get("enhanced_avg_speed")
    if not isinstance(v, (int, float)):
        v = se.get("avg_speed")
    return v if isinstance(v, (int, float)) else None


def _session_cadence(se: dict):
    v = se.get("avg_running_cadence")
    if not isinstance(v, (int, float)):
        v = se.get("avg_cadence")
    return v if isinstance(v, (int, float)) else None


def _multisport_sub_sport(distinct: set[str]) -> Optional[str]:
    """Derive the discipline label from the sports involved."""
    if {"swimming", "cycling", "running"} <= distinct:
        return "triathlon"
    if distinct == {"cycling", "running"}:
        return "duathlon"
    if distinct == {"swimming", "running"}:
        return "aquathlon"
    return None


def _aggregate_multisport(sessions: list[dict], distinct: set[str]) -> dict:
    """Combines the individual legs (incl. transitions) of a multisport event
    into overall metrics: sums for additive quantities, weighted
    means (by leg duration) for averages, maxima for the peak values."""

    def _vals(getter):
        return [g for se in sessions if (g := getter(se)) is not None]

    def _sum(getter):
        vals = _vals(getter)
        return sum(vals) if vals else None

    def _max(getter):
        vals = _vals(getter)
        return max(vals) if vals else None

    def _wavg(getter):
        num = den = 0.0
        for se in sessions:
            v, w = getter(se), _session_dur(se)
            if isinstance(v, (int, float)) and isinstance(w, (int, float)) and w > 0:
                num += v * w
                den += w
        return (num / den) if den else None

    def _num(field):
        return lambda se: se.get(field) if isinstance(se.get(field), (int, float)) else None

    def _total_tss():
        """TSS only if EVERY non-transition leg brings one — otherwise None.

        Garmin writes training_stress_score per leg and often only for some:
        the brick duathlon of 2026-08-08 has 19 sessions (10 legs + 9
        transitions) and TSS on exactly ONE cycling leg (26.4). The sum would then
        not be the total load but the value of a single leg — and
        because it is > 0, fitness.effective_tss would take it for a real
        value and no longer estimate from heart rate. A 4:15 h duathlon
        would thus enter the form curve with 26 TSS. Without a value MFC estimates
        the whole multisport as hrTSS — summing incompletely is worse than not
        summing at all."""
        legs = [se for se in sessions
                if _enum_name(se.get("sport"), _SPORT_NAMES) != "transition"]
        vals = [se.get("training_stress_score") for se in legs]
        if not legs or not all(isinstance(v, (int, float)) for v in vals):
            return None
        return sum(vals)

    starts = [se.get("start_time") for se in sessions
              if isinstance(se.get("start_time"), datetime)]
    descs = [_clean_text(se.get("description")) for se in sessions]

    _dur, _elapsed = _sum(_session_dur), _sum(_session_elapsed)
    if (isinstance(_dur, (int, float)) and isinstance(_elapsed, (int, float))
            and _dur > _elapsed):  # moving never > total time
        _dur = _elapsed
    return {
        "sport": "multisport",
        "sub_sport": _multisport_sub_sport(distinct),
        "start_time": min(starts) if starts else None,
        "duration_s": _as_int(_dur),
        "elapsed_s": _as_int(_elapsed),
        "distance_m": _as_float(_sum(_num("total_distance"))),
        "calories": _as_int(_sum(_num("total_calories"))),
        "avg_hr": _as_int(_wavg(_num("avg_heart_rate"))),
        "max_hr": _as_int(_max(_num("max_heart_rate"))),
        "avg_power": _as_int(_wavg(_num("avg_power"))),
        "max_power": _as_int(_max(_num("max_power"))),
        "norm_power": _as_int(_wavg(_num("normalized_power"))),
        "avg_speed_ms": _as_float(_wavg(_session_speed)),
        "total_ascent_m": _as_int(_sum(_num("total_ascent"))),
        "total_descent_m": _as_int(_sum(_num("total_descent"))),
        "avg_cadence": _as_int(_wavg(_session_cadence)),
        "total_strides": _as_int(_sum(_num("total_strides"))),
        "tss": _as_float(_total_tss()),
        "description": next((d for d in descs if d), None),
    }


def parse_sessions(data: bytes) -> list[dict]:
    """The raw `session` messages (one entry per leg, transitions included).

    Basis for the TSS estimate: it needs the single leg AND the
    athlete (FTP, threshold pace, CSS) — athlete knowledge does not belong in the
    parser, so it only returns the raw data (see tss_estimate)."""
    return _first_messages(data)["sessions"]


def parse_fit(data: bytes) -> FitMetrics:
    """Extracts metrics from FIT bytes. Raises FitParseError only if the
    file is not readable as FIT; missing single fields yield None.

    Multisport (several `session` messages with different sports, e.g.
    triathlon) is detected and aggregated over all legs (sport=multisport)
    instead of taking only the first leg."""
    m = _first_messages(data)
    file_id, s, workout, first_gps = m["file_id"], m["session"], m["workout"], m["first_gps"]
    dev, fc = m["device_info"], m["file_creator"]
    if not file_id and not s:
        # A valid FIT has at least one file_id message; if even that is missing
        # (e.g. empty file), it is not a usable FIT file.
        raise FitParseError("no FIT messages found")

    _manufacturer = _clean_text(file_id.get("manufacturer"))
    _product = _model_product(file_id, m["device_creator"])
    from . import devfields
    _produced_by = devfields.producer_from_dev_ids(m["dev_apps"])
    _sw = dev.get("software_version") or fc.get("software_version")
    _serial = _as_int(file_id.get("serial_number")) or _as_int(dev.get("serial_number"))

    start = s.get("start_time") or file_id.get("time_created")
    # Moving (net, = Strava "Moving Time"):
    #  - pool swimming: sum of the active lengths — subtracts the pauses between lengths.
    #    The timer does NOT do that (timer≈elapsed), and total_moving_time is missing in
    #    older FITs; the length sum reconstructs the real moving time.
    #  - otherwise: total_moving_time (>0, if present), then timer, then elapsed.
    duration = None
    _sa = m["swim_active_s"]
    if _sa["n"] > 0:
        duration = _sa["sum"]
    if duration is None:
        _mv = s.get("total_moving_time")
        if isinstance(_mv, (int, float)) and _mv > 0:
            duration = _mv
    if duration is None:
        duration = s.get("total_timer_time")
    if duration is None:
        duration = s.get("total_elapsed_time")
    elapsed = s.get("total_elapsed_time")
    if elapsed is None:
        elapsed = s.get("total_timer_time")
    # Moving can never be longer than the total time — for some trainer/
    # virtual activities total_moving_time is a few seconds above
    # total_elapsed_time due to FIT rounding; cap at elapsed.
    if (isinstance(duration, (int, float)) and isinstance(elapsed, (int, float))
            and duration > elapsed):
        duration = elapsed
    speed = s.get("enhanced_avg_speed")
    if speed is None:
        speed = s.get("avg_speed")
    cadence = s.get("avg_running_cadence")
    if cadence is None:
        cadence = s.get("avg_cadence")
    _start_lat = _coord(s.get("start_position_lat"), 90)
    _start_lng = _coord(s.get("start_position_long"), 180)
    if (_start_lat is None or _start_lng is None) and first_gps:
        # session without start_position -> take the first GPS record
        _start_lat = _coord(first_gps.get("lat"), 90)
        _start_lng = _coord(first_gps.get("long"), 180)
    if _start_lat == 0 and _start_lng == 0:  # 0/0 is not a real start point
        _start_lat = _start_lng = None

    avg_power, max_power = _power_fallback(
        _as_int(s.get("avg_power")), _as_int(s.get("max_power")), m["record_power"]
    )

    metrics = FitMetrics(
        sport=_enum_name(s.get("sport"), _SPORT_NAMES),
        sub_sport=_enum_name(s.get("sub_sport"), _SUB_SPORT_NAMES),
        start_time=start if isinstance(start, datetime) else None,
        duration_s=_as_int(duration),
        elapsed_s=_as_int(elapsed),
        distance_m=_as_float(s.get("total_distance")),
        calories=_as_int(s.get("total_calories")),
        avg_hr=_as_int(s.get("avg_heart_rate")),
        max_hr=_as_int(s.get("max_heart_rate")),
        avg_power=avg_power,
        max_power=max_power,
        norm_power=_np_fallback(_as_int(s.get("normalized_power")),
                                m["record_power"]["samples"]),
        avg_speed_ms=_as_float(speed),
        total_ascent_m=_as_int(s.get("total_ascent")),
        total_descent_m=_as_int(s.get("total_descent")),
        avg_cadence=_as_int(cadence),
        total_strides=_as_int(s.get("total_strides")),
        tss=_as_float(s.get("training_stress_score")),
        device=_device_str(_manufacturer, _product),
        serial_number=_serial,
        manufacturer=_manufacturer,
        product=_product,
        produced_by=_produced_by,
        software_version=_clean_text(_sw),
        title=_clean_text(workout.get("wkt_name")),
        description=_clean_text(s.get("description")),
        start_lat=_start_lat,
        start_lng=_start_lng,
    )

    # Multisport: several legs with different sports -> aggregate.
    sessions = m["sessions"]
    distinct = {
        sp for se in sessions
        if (sp := _enum_name(se.get("sport"), _SPORT_NAMES))
        and sp != "transition"
    }
    is_multisport = (
        str(file_id.get("type")) == "multisport" or len(distinct) > 1
    )
    if is_multisport and sessions:
        for field, value in _aggregate_multisport(sessions, distinct).items():
            setattr(metrics, field, value)

    return metrics
