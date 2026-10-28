"""Best efforts + auto splits from the FULL-resolution record messages.

Runs inside `streams.extract_streams` (one parse, one cache entry in
MyFITContainer), because the downsampled curves (<= 1500 points) are too coarse
for peak values: at 7 s per point the stride swallows the 5 s peak power and
places kilometer boundaries up to 7 s off.

- Peak power over fixed windows (5 s … 60 min) + mean-max curve on a
  1 Hz timeline; pauses count as 0 W (elapsed basis, like TP/Golden
  Cheetah).
- Fastest distances (100 m … marathon per sport; none for cycling, where only
  power counts) with linearly interpolated crossing time - records are
  only available per second. Which of them RANK is decided by
  `best_store.RANKED` (in MyFITContainer) per group (indoor/outdoor,
  pool/open water).
- Pool swimming computes from the `length` messages (`fastest_pool_distances`),
  not from the records: those carry the distance only at the end of a length
  and, after a pause, several lengths with the same timestamp (prod: 100 m in 0.4 s).
- Split boundaries (1 km running, 5 km cycling, 250 m open water) as second offsets;
  the per-split metrics are computed by `streams._laps_from_bounds`, so that splits
  and laps get the same table/overlays.
"""

from __future__ import annotations

from datetime import datetime
from statistics import median

_PEAK_S = ((5, "5 s"), (30, "30 s"), (60, "1 min"), (300, "5 min"),
           (600, "10 min"), (1200, "20 min"), (3600, "60 min"))
_CURVE_S = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 420, 600,
            900, 1200, 1800, 2400, 3600, 5400, 7200, 10800, 14400)
_MAX_LINE_S = 24 * 3600      # cap longer timelines (outlier timestamps)

_DIST_M = {
    "running": ((100, "100 m"), (200, "200 m"), (400, "400 m"), (800, "800 m"),
                (1000, "1 km"), (5000, "5 km"), (10000, "10 km"),
                (21097, "Half marathon"), (42195, "Marathon")),
    "cycling": (),
    "swimming": ((50, "50 m"), (100, "100 m"), (200, "200 m"), (400, "400 m"),
                 (750, "750 m"), (800, "800 m"), (1000, "1000 m"),
                 (1500, "1500 m"), (1900, "1900 m"), (3800, "3800 m")),
}
_DIST_DEFAULT = ((1000, "1 km"), (5000, "5 km"))
_SPLIT_M = {"running": 1000.0, "cycling": 5000.0, "swimming": 250.0}
_SPLIT_DEFAULT = 1000.0
_MIN_REST_M = 100.0          # a shorter rest after the last full split is dropped
_MAX_SWIM_MS = 2.0           # faster than the 50 m world record (~2.1 m/s) is no length
_FRAGMENT_T = 0.6            # length under 60 % of median time with fewer strokes than median


def _series(recs: list[dict], t0: datetime, key: str) -> list[tuple[float, float]]:
    """[(second, value)] for records with a numeric `key`, ascending."""
    out = []
    for r in recs:
        ts, v = r.get("timestamp"), r.get(key)
        if isinstance(ts, datetime) and isinstance(v, (int, float)):
            out.append(((ts - t0).total_seconds(), float(v)))
    return out


# ---------- Peak power / mean-max ----------

def _timeline(pts: list[tuple[float, float]]) -> list[float]:
    """1 Hz timeline from second 0; gaps stay 0 (pause)."""
    end = min(int(pts[-1][0]) + 1, _MAX_LINE_S)
    line = [0.0] * end
    for sec, v in pts:
        i = int(sec)
        if 0 <= i < end:
            line[i] = v
    return line


def _mean_max(prefix: list[float], d: int) -> float | None:
    n = len(prefix) - 1
    if d <= 0 or d > n:
        return None
    best = 0.0
    for i in range(n - d + 1):
        s = prefix[i + d] - prefix[i]
        if s > best:
            best = s
    return best / d


def power_peaks(recs: list[dict], t0: datetime) -> dict | None:
    """{"peaks": [{s, label, w}], "curve": [[s, w], …]} or None without power."""
    pts = _series(recs, t0, "power")
    if len(pts) < 5 or not any(v > 0 for _, v in pts):
        return None
    line = _timeline(pts)
    prefix = [0.0]
    for v in line:
        prefix.append(prefix[-1] + v)
    peaks = []
    for d, label in _PEAK_S:
        w = _mean_max(prefix, d)
        if w is not None:
            peaks.append({"s": d, "label": label, "w": round(w)})
    curve = []
    for d in _CURVE_S:
        w = _mean_max(prefix, d)
        if w is not None:
            curve.append([d, round(w)])
    return {"peaks": peaks, "curve": curve}


# ---------- Fastest distances ----------

def _cross_time(pts, i, j, target: float) -> float:
    """Time at which exactly `target` meters have been covered from pts[i] - linearly
    interpolated between pts[j-1] and pts[j]."""
    t_prev, d_prev = pts[j - 1][0], pts[j - 1][1] - pts[i][1]
    t_j, d_j = pts[j][0], pts[j][1] - pts[i][1]
    if d_j <= d_prev:
        return t_j
    return t_prev + (target - d_prev) / (d_j - d_prev) * (t_j - t_prev)


def _start_time(pts, i, j, target: float) -> float:
    """Time at which exactly `target` meters remain up to pts[j] - linearly
    interpolated between pts[i] and pts[i+1] (pts[i] lies before it)."""
    d_goal = pts[j][1] - target
    t_i, d_i = pts[i]
    t_n, d_n = pts[i + 1]
    if d_n <= d_i:
        return t_n
    return t_i + (d_goal - d_i) / (d_n - d_i) * (t_n - t_i)


def fastest_distances(recs: list[dict], t0: datetime, sport: str | None) -> list[dict]:
    """[{m, label, s}] per standard distance that fits into the activity.

    Distance is taken as linear between two records; the shortest window then
    starts at a record (end interpolated) OR ends at one (start
    interpolated). Both passes are needed: with smart recording (every 6 s)
    the start would otherwise be up to one interval too late - 100 m in
    14 instead of 20 s."""
    pts = _series(recs, t0, "distance")
    if len(pts) < 2:
        return []
    total = pts[-1][1] - pts[0][1]
    out = []
    for target, label in _DIST_M.get(sport or "", _DIST_DEFAULT):
        if target > total:
            break
        best = None
        j = 0
        for i in range(len(pts)):                    # start at a record
            while j < len(pts) and pts[j][1] - pts[i][1] < target:
                j += 1
            if j >= len(pts):
                break
            t = _cross_time(pts, i, j, target) - pts[i][0]
            if t > 0 and (best is None or t < best):
                best = t
        i = 0
        for j in range(1, len(pts)):                 # end at a record
            while i + 1 < j and pts[j][1] - pts[i + 1][1] >= target:
                i += 1
            if pts[j][1] - pts[i][1] < target:
                continue
            t = pts[j][0] - _start_time(pts, i, j, target)
            if t > 0 and (best is None or t < best):
                best = t
        if best:
            out.append({"m": target, "label": label, "s": round(best, 1)})
    return out


def fastest_pool_distances(lengths: list[dict], pool_m: float | None) -> list[dict]:
    """Pool swimming: [{m, label, s}] from the length messages. A window is
    k = distance/pool length CONSECUTIVE active lengths with no pause (idle
    length) in between; time = sum of the length times. Distances that do
    not divide into whole lengths are dropped.

    Also breaking the series: drill lengths (in drill mode the watch detects
    no lengths, the distance is typed in afterwards, and it invents lengths
    of 0.009 s or 2 s for it - Fenix 8, FR945 and FORM alike), phantom
    lengths faster than `_MAX_SWIM_MS` (50 m in 12.6 s with two strokes:
    misdetection at the push-off) and fragments: on a stroke change the
    watch splits ONE length into two (14.9 s/6 strokes + 14.8 s/7 strokes =
    one length of 28 s/13 strokes). A fragment is detectable relative to the
    session: under `_FRAGMENT_T` of the median time AND fewer strokes than
    the median - a real sprint has MORE strokes, not fewer."""
    if not isinstance(pool_m, (int, float)) or pool_m <= 0:
        return []
    raw: list[tuple[float, float | None] | None] = []   # (seconds, strokes) per length
    for L in lengths:
        v = L.get("total_elapsed_time")
        if not isinstance(v, (int, float)):
            v = L.get("total_timer_time")
        k = L.get("total_strokes")
        ok = (str(L.get("length_type") or "active") == "active"
              and str(L.get("swim_stroke") or "") != "drill"
              and isinstance(v, (int, float)) and v > 0 and pool_m / v <= _MAX_SWIM_MS)
        raw.append((float(v), float(k) if isinstance(k, (int, float)) else None) if ok else None)
    valid = [x for x in raw if x is not None]
    if not valid:
        return []
    med_t = median(v for v, _ in valid)
    ks = [k for _, k in valid if k is not None]
    med_k = median(ks) if ks else None
    secs: list[float | None] = []                    # None = pause / unusable
    for x in raw:
        if x is not None and med_k is not None and x[1] is not None \
                and x[0] < _FRAGMENT_T * med_t and x[1] < med_k:
            x = None                                 # fragment of a split length
        secs.append(x[0] if x is not None else None)
    out = []
    for target, label in _DIST_M["swimming"]:
        k = target / float(pool_m)
        if abs(k - round(k)) > 1e-6:
            continue
        k = int(round(k))
        if k > len(secs):
            break
        best = None
        acc, start = 0.0, 0
        for i, v in enumerate(secs):
            if v is None:
                acc, start = 0.0, i + 1
                continue
            acc += v
            if i - start + 1 > k:
                acc -= secs[start]
                start += 1
            if i - start + 1 == k and (best is None or acc < best):
                best = acc
        if best:
            out.append({"m": target, "label": label, "s": round(best, 1)})
    return out


# ---------- Auto-Splits ----------

def split_bounds(recs: list[dict], t0: datetime, sport: str | None,
                 end_s: float) -> tuple[list[float], dict, float, dict | None]:
    """(boundaries in seconds [0, …, end], names per split, split length, rest).
    The last, incomplete split keeps its real length in the name. A rest
    under 100 m does not become a split (23 m in 11 s would give a meaningless
    pace) but is reported as {dist_m, dur_s}, so the table mentions it. Empty
    if no full split length is reached."""
    pts = _series(recs, t0, "distance")
    if len(pts) < 2:
        return [], {}, 0.0, None
    step = _SPLIT_M.get(sport or "", _SPLIT_DEFAULT)
    d0 = pts[0][1]
    total = pts[-1][1] - d0
    if total < step:
        return [], {}, step, None
    bounds = [0.0]
    names: dict = {}
    k = 1
    for j in range(1, len(pts)):
        while pts[j][1] - d0 >= k * step:
            bounds.append(round(_cross_time(pts, 0, j, k * step), 1))
            names[str(k)] = _dist_label(k * step)
            k += 1
    rest = None
    if end_s > bounds[-1] + 1:
        rest_m = total - (k - 1) * step
        if rest_m >= _MIN_REST_M:                    # rest as the last split
            bounds.append(round(end_s, 1))
            names[str(k)] = _dist_label(total)
        else:
            rest = {"dist_m": round(rest_m, 1), "dur_s": round(end_s - bounds[-1])}
    return bounds, names, step, rest


def _dist_label(m: float) -> str:
    if m < 1000:
        return f"{round(m)} m"
    km = m / 1000.0
    return f"{km:g} km" if abs(km - round(km)) < 1e-6 else f"{km:.2f} km"
