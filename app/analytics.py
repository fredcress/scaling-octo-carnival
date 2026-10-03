"""Pure computations: per-activity metrics and cross-activity models.

Conventions: distances in metres, times in seconds, speeds in m/s, HR in bpm.
"""
import math
from datetime import date, timedelta

BEST_EFFORT_DISTANCES = {
    1000: "1 km",
    1609.344: "1 mile",
    5000: "5 km",
    10000: "10 km",
    21097.5: "Half marathon",
    42195: "Marathon",
}

ZONE_BOUNDS = (0.5, 0.6, 0.7, 0.8, 0.9)  # lower edge of Z1..Z5 as fraction of HR reserve
MAX_GAP_S = 10  # longer gaps between samples are pauses, not effort


def hr_zone_limits(max_hr: float, resting_hr: float) -> list[int]:
    """Lower bpm bound of zones 1..5 (Karvonen / heart-rate reserve)."""
    reserve = max_hr - resting_hr
    return [round(resting_hr + f * reserve) for f in ZONE_BOUNDS]


def zone_of(hr: float, limits: list[int]) -> int:
    """0-based zone index; anything under Z1 counts as Z1."""
    z = 0
    for i, lo in enumerate(limits):
        if hr >= lo:
            z = i
    return z


def time_in_zones(t: list, hr: list, limits: list[int]) -> list[float] | None:
    out = [0.0] * len(limits)
    seen = False
    for i in range(len(t) - 1):
        if hr[i] is None:
            continue
        dt = min(t[i + 1] - t[i], MAX_GAP_S)
        if dt <= 0:
            continue
        out[zone_of(hr[i], limits)] += dt
        seen = True
    return [round(x) for x in out] if seen else None


def trimp(t: list, hr: list, max_hr: float, resting_hr: float, sex: str) -> float | None:
    """Banister TRIMP summed sample by sample."""
    a, b = (0.86, 1.67) if sex.startswith("f") else (0.64, 1.92)
    reserve = max_hr - resting_hr
    total = 0.0
    seen = False
    for i in range(len(t) - 1):
        if hr[i] is None:
            continue
        dt = min(t[i + 1] - t[i], MAX_GAP_S)
        if dt <= 0:
            continue
        x = min(max((hr[i] - resting_hr) / reserve, 0.0), 1.0)
        total += dt / 60 * x * a * math.exp(b * x)
        seen = True
    return round(total, 1) if seen else None


def estimated_trimp(moving_s: float, sex: str) -> float:
    """Fallback load when there is no HR: assume a steady easy effort (60% HRR)."""
    a, b = (0.86, 1.67) if sex.startswith("f") else (0.64, 1.92)
    return round(moving_s / 60 * 0.6 * a * math.exp(b * 0.6), 1)


def best_efforts(t: list, d: list) -> dict[float, tuple[float, float]]:
    """Fastest time over each standard distance, from the cumulative distance stream.
    Returns {distance: (time_s, start_offset_s)}."""
    pts = [(ti, di) for ti, di in zip(t, d) if di is not None]
    out = {}
    if len(pts) < 2:
        return out
    total = pts[-1][1] - pts[0][1]
    for target in BEST_EFFORT_DISTANCES:
        if total < target:
            continue
        best = None
        j = 0
        for i in range(1, len(pts)):
            # advance the window start while it still covers the target distance
            while j + 1 < i and pts[i][1] - pts[j + 1][1] >= target:
                j += 1
            covered = pts[i][1] - pts[j][1]
            if covered >= target:
                # scale to the exact distance to remove the overshoot of the last sample
                elapsed = (pts[i][0] - pts[j][0]) * target / covered
                if best is None or elapsed < best[0]:
                    best = (elapsed, pts[j][0])
        if best:
            out[target] = (round(best[0], 1), best[1])
    return out


def km_splits(t: list, d: list, hr: list, alt: list) -> list[dict]:
    """Per-kilometre splits (last partial split included if >= 100 m)."""
    splits = []
    if not d or all(x is None for x in d):
        return splits
    start_i = 0
    next_km = 1000.0
    n = len(t)
    for i in range(1, n):
        if d[i] is None:
            continue
        last = i == n - 1
        if d[i] >= next_km or last:
            seg_d = d[i] - (d[start_i] or 0)
            if last and seg_d < 100 and d[i] < next_km:
                break
            seg_t = t[i] - t[start_i]
            hrs = [h for h in hr[start_i:i + 1] if h]
            gain = 0.0
            alts = [a for a in alt[start_i:i + 1] if a is not None]
            for k in range(1, len(alts)):
                if alts[k] > alts[k - 1]:
                    gain += alts[k] - alts[k - 1]
            splits.append({
                "km": len(splits) + 1,
                "distance_m": round(seg_d, 1),
                "time_s": round(seg_t, 1),
                "pace_s_per_km": round(seg_t / seg_d * 1000, 1) if seg_d > 0 else None,
                "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
                "gain_m": round(gain, 1),
            })
            start_i = i
            next_km += 1000.0
    return splits


def downsample(n: int, max_points: int) -> list[int]:
    if n <= max_points:
        return list(range(n))
    step = n / max_points
    idx = [int(i * step) for i in range(max_points)]
    if idx[-1] != n - 1:
        idx.append(n - 1)
    return idx


# ---------------------------------------------------------------- cross-activity

def fitness_series(daily_load: dict[str, float], start: date, end: date) -> list[dict]:
    """Banister impulse-response: fitness (CTL, 42 d), fatigue (ATL, 7 d),
    form (TSB) = yesterday's fitness - yesterday's fatigue."""
    ctl = atl = 0.0
    out = []
    day = start
    while day <= end:
        load = daily_load.get(day.isoformat(), 0.0)
        tsb = ctl - atl
        ctl += (load - ctl) / 42
        atl += (load - atl) / 7
        out.append({"date": day.isoformat(), "load": round(load, 1),
                    "ctl": round(ctl, 1), "atl": round(atl, 1), "tsb": round(tsb, 1)})
        day += timedelta(days=1)
    return out


def riegel(time_s: float, from_m: float, to_m: float, exponent: float = 1.06) -> float:
    return time_s * (to_m / from_m) ** exponent
