"""Training plan generator for a target race. Pure functions, no I/O.

Paces come from Jack Daniels' VDOT model; the structure is a classic
base -> build -> peak -> taper progression with a lighter week every fourth week
and weekly volume rising by at most ~10 %.

Conventions as in analytics: metres, seconds, s/km for paces; weekday 0 = Monday.
"""
import math
from datetime import date, timedelta

RACE_DISTANCES = {
    5000: "5 km",
    10000: "10 km",
    21097.5: "Half marathon",
    42195: "Marathon",
}

# peak weekly km at 5 runs/week, scaled by FREQ_FACTOR for other frequencies
PEAK_KM = {5000: 35, 10000: 42, 21097.5: 50, 42195: 65}
FREQ_FACTOR = {3: 0.8, 4: 0.9, 5: 1.0, 6: 1.1}
PEAK_LONG_KM = {5000: 12, 10000: 16, 21097.5: 19, 42195: 32}
LONG_SHARE = {3: 0.38, 4: 0.33, 5: 0.30, 6: 0.27}  # long run as share of the week
TAPER_WEEKS = {5000: 1, 10000: 1, 21097.5: 1, 42195: 2}  # before race week
WEEKLY_GROWTH = 1.10
LONG_STEP_KM = 2.0
DEFAULT_VDOT = 33.0  # no data and no goal: a gentle beginner level
MIN_EASY_KM = 3.0

# fraction of VO2max each training pace is run at (Daniels)
PACE_FRACTIONS = {"easy_slow": 0.62, "easy": 0.70, "marathon": 0.81,
                  "threshold": 0.88, "interval": 0.975}

# Day offsets relative to the long run, by runs per week: the long run sits on
# the chosen day, quality sessions mid-week, easy runs fill the gaps.
DAY_TEMPLATE = {
    3: [(-5, "q1"), (-3, "easy"), (0, "long")],
    4: [(-5, "q1"), (-3, "q2"), (-1, "easy"), (0, "long")],
    5: [(-6, "easy"), (-5, "q1"), (-3, "q2"), (-1, "easy"), (0, "long")],
    6: [(-6, "easy"), (-5, "q1"), (-4, "easy"), (-3, "q2"), (-1, "easy"), (0, "long")],
}

WARMUP_KM = 2.0
COOLDOWN_KM = 1.5


# ---------------------------------------------------------------- VDOT

def _vo2_at(v_m_per_min: float) -> float:
    return -4.60 + 0.182258 * v_m_per_min + 0.000104 * v_m_per_min ** 2


def _fraction_sustained(t_min: float) -> float:
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t_min) + 0.2989558 * math.exp(-0.1932605 * t_min)


def vdot(distance_m: float, time_s: float) -> float:
    t_min = time_s / 60
    return _vo2_at(distance_m / t_min) / _fraction_sustained(t_min)


def race_time(distance_m: float, vdot_value: float) -> float:
    """Inverse of vdot(): the time a runner of this VDOT should race the distance in."""
    lo, hi = distance_m / 1000 * 120, distance_m / 1000 * 1200  # 2:00 .. 20:00 /km
    for _ in range(60):
        mid = (lo + hi) / 2
        if vdot(distance_m, mid) > vdot_value:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def pace_at(vdot_value: float, fraction: float) -> float:
    """s/km of the speed at which the oxygen cost equals `fraction` of VO2max."""
    a, b, c = 0.000104, 0.182258, -4.60 - fraction * vdot_value
    v = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)  # m/min
    return 60000 / v


def training_paces(vdot_train: float, race_pace: float) -> dict:
    return {
        "easy": [round(pace_at(vdot_train, PACE_FRACTIONS["easy"])),
                 round(pace_at(vdot_train, PACE_FRACTIONS["easy_slow"]))],
        "marathon": round(pace_at(vdot_train, PACE_FRACTIONS["marathon"])),
        "threshold": round(pace_at(vdot_train, PACE_FRACTIONS["threshold"])),
        "interval": round(pace_at(vdot_train, PACE_FRACTIONS["interval"])),
        "race": round(race_pace),
    }


# ---------------------------------------------------------------- structure

def split_phases(n_weeks: int, distance_m: float) -> list[str]:
    """Phase name for each week; the last week is always the race week."""
    if n_weeks <= 1:
        return ["race"]
    taper = min(TAPER_WEEKS[distance_m], n_weeks - 1)
    rest = n_weeks - 1 - taper
    base = max(1, round(rest * 0.4)) if rest >= 3 else 0
    peak = min(3, round(rest * 0.2)) if rest >= 5 else 0
    build = rest - base - peak
    return ["base"] * base + ["build"] * build + ["peak"] * peak + ["taper"] * taper + ["race"]


def _round_half(x: float) -> float:
    return round(x * 2) / 2


def _fmt_pace(s_per_km: float) -> str:
    s = round(s_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


def _easy_range(paces: dict) -> str:
    fast, slow = paces["easy"]
    return f"{_fmt_pace(fast)[:-3]}–{_fmt_pace(slow)}"


def _session(day: date, kind: str, title: str, detail: str, km: float) -> dict:
    return {"date": day.isoformat(), "type": kind, "title": title, "detail": detail,
            "distance_km": _round_half(km)}


def _reps_session(day, kind, title, reps, rep_km, rec_km, pace, rec_text):
    km = WARMUP_KM + reps * rep_km + (reps - 1) * rec_km + COOLDOWN_KM
    rep = f"{rep_km * 1000:.0f} m" if rep_km < 1 or rep_km % 1 else f"{rep_km:.0f} km"
    detail = (f"{WARMUP_KM:.0f} km easy, {reps} × {rep} at {_fmt_pace(pace)} "
              f"with {rec_text}, {COOLDOWN_KM:g} km easy")
    return _session(day, kind, title, detail, km)


def _tempo_session(day, reps, minutes, paces):
    t = paces["threshold"]
    rep_km = minutes * 60 / t
    km = WARMUP_KM + reps * rep_km + (reps - 1) * 0.3 + COOLDOWN_KM
    work = f"{reps} × {minutes} min" if reps > 1 else f"{minutes} min continuous"
    rec = " with 2 min jog" if reps > 1 else ""
    return _session(day, "threshold", "Threshold",
                    f"{WARMUP_KM:.0f} km easy, {work} at {_fmt_pace(t)}{rec}, {COOLDOWN_KM:g} km easy", km)


THRESHOLD_STEPS = [(3, 6), (3, 8), (4, 8), (3, 10), (2, 15), (4, 10), (1, 25), (3, 12)]
INTERVAL_STEPS = [(5, 0.8), (6, 0.8), (5, 1.0), (6, 1.0), (5, 1.2), (4, 1.6)]
RACE_PACE_STEPS = {
    5000: [(5, 1.0), (4, 1.2), (3, 1.6)],
    10000: [(4, 2.0), (5, 2.0), (3, 3.0)],
    21097.5: [(3, 3.0), (3, 4.0), (2, 5.0)],
    42195: [(2, 5.0), (3, 5.0), (2, 8.0)],
}


def _easy(day, km, paces, strides=False):
    detail = f"Conversational, {_easy_range(paces)}"
    if strides:
        detail += ", then 6 × 20 s strides (fast but relaxed, walk back)"
    return _session(day, "easy", "Easy + strides" if strides else "Easy", detail, km)


def _long(day, km, phase, phase_week, distance_m, paces):
    base = f"Steady, {_easy_range(paces)}"
    if phase == "peak":
        if distance_m >= 21097.5:
            fast = min(km * 0.5, (5 if distance_m < 42195 else 12) + 2 * phase_week)
            return _session(day, "long", "Long run, race-pace finish",
                            f"{base}, the last {fast:.0f} km at race pace {_fmt_pace(paces['race'])}", km)
        return _session(day, "long", "Long run, fast finish",
                        f"{base}, the last 2–3 km at {_fmt_pace(paces['threshold'])}", km)
    return _session(day, "long", "Long run", base, km)


def _q1(day, phase, phase_week, build_week, distance_m, paces, recovery):
    if phase == "base":
        if phase_week < 2 or recovery:
            return None  # becomes easy + strides
        return _tempo_session(day, *THRESHOLD_STEPS[0], paces)
    if phase == "build":
        if recovery:
            return _tempo_session(day, *THRESHOLD_STEPS[0], paces)
        return _tempo_session(day, *THRESHOLD_STEPS[min(build_week + 1, len(THRESHOLD_STEPS) - 1)], paces)
    if phase == "peak":
        steps = RACE_PACE_STEPS[distance_m]
        reps, rep_km = steps[min(phase_week, len(steps) - 1)]
        return _reps_session(day, "race_pace", "Race pace", reps, rep_km, 0.4, paces["race"], "400 m jog")
    if phase == "taper":
        reps, rep_km = RACE_PACE_STEPS[distance_m][0]
        reps = max(2, reps - 2)
        return _reps_session(day, "race_pace", "Race pace, short", reps, rep_km, 0.4, paces["race"],
                             "400 m jog")
    return None


def _q2(day, phase, phase_week, build_week, distance_m, paces, recovery):
    if recovery or phase in ("taper", "race"):
        return None
    if phase == "base":
        if phase_week < 2:
            return None
        i = paces["interval"]
        return _session(day, "fartlek", "Fartlek",
                        f"{WARMUP_KM:.0f} km easy, 8 × 1 min brisk (about {_fmt_pace(i)}) / 1 min easy, "
                        f"{COOLDOWN_KM:g} km easy", WARMUP_KM + 3.5 + COOLDOWN_KM)
    if distance_m == 42195 and phase == "peak":
        return _tempo_session(day, *THRESHOLD_STEPS[3], paces)  # q1 and the long run carry race pace
    if distance_m == 42195:
        km = 8 + 2 * min(build_week, 4)
        return _session(day, "race_pace", "Marathon pace",
                        f"{WARMUP_KM:.0f} km easy, {km - 4:.0f} km at {_fmt_pace(paces['marathon'])}, "
                        f"{WARMUP_KM:.0f} km easy", km)
    reps, rep_km = INTERVAL_STEPS[min(build_week, len(INTERVAL_STEPS) - 1)]
    if phase == "peak":
        reps, rep_km = INTERVAL_STEPS[2]  # keep the engine ticking, lighter than build
    return _reps_session(day, "intervals", "Intervals", reps, rep_km, 0.4, paces["interval"],
                         "2–3 min easy jog")


def _race_week(week_start: date, race_day: date, runs: int, distance_m: float, paces: dict,
               race_name: str) -> list[dict]:
    out = []
    candidates = [
        (-4, _session(race_day - timedelta(days=4), "race_pace", "Sharpener",
                      f"{WARMUP_KM:.0f} km easy, 4 × 1 min at race pace {_fmt_pace(paces['race'])} "
                      f"with 2 min jog, {COOLDOWN_KM:g} km easy", 6)),
        (-2, _session(race_day - timedelta(days=2), "easy", "Easy + strides",
                      f"20–30 min at {_easy_range(paces)}, 4 × 20 s strides", 4)),
    ]
    if runs >= 5:
        candidates.append((-6, _easy(race_day - timedelta(days=6), 6, paces)))
    if runs >= 6:
        candidates.append((-1, _session(race_day - timedelta(days=1), "easy", "Shake-out",
                                        "15 min very easy, a few strides", 2.5)))
    for _, s in sorted(candidates, key=lambda c: c[0]):
        if date.fromisoformat(s["date"]) >= week_start:
            out.append(s)
    goal = f"goal pace {_fmt_pace(paces['race'])}"
    out.append(_session(race_day, "race", race_name or RACE_DISTANCES[distance_m],
                        f"{RACE_DISTANCES[distance_m]} race · {goal}. Start controlled, "
                        "speed up in the second half if you feel good.", distance_m / 1000))
    return out


# ---------------------------------------------------------------- generator

def generate(cfg: dict) -> dict:
    """Build the plan from a saved config:
    race_date, distance_m, goal_time_s (or None), runs_per_week, long_run_day,
    start_date, race_name, baseline = {week_km, long_km, vdot}."""
    race_day = date.fromisoformat(cfg["race_date"])
    start = date.fromisoformat(cfg["start_date"])
    distance_m = float(cfg["distance_m"])
    runs = int(cfg["runs_per_week"])
    long_day = int(cfg["long_run_day"])
    base = cfg["baseline"]

    vdot_current = base.get("vdot")
    goal_time = cfg.get("goal_time_s")
    vdot_goal = vdot(distance_m, goal_time) if goal_time else None
    vdot_train = vdot_current or vdot_goal or DEFAULT_VDOT
    if vdot_goal:
        race_pace = goal_time / distance_m * 1000
    else:
        race_pace = race_time(distance_m, vdot_train) / distance_m * 1000
    paces = training_paces(vdot_train, race_pace)

    # set up from Thursday on, the plan starts next Monday rather than with a stub week
    next_monday = start + timedelta(days=7 - start.weekday())
    if start.weekday() >= 3 and next_monday + timedelta(days=7) <= race_day:
        start = next_monday
    first_monday = start - timedelta(days=start.weekday())
    race_monday = race_day - timedelta(days=race_day.weekday())
    n_weeks = (race_monday - first_monday).days // 7 + 1
    phases = split_phases(n_weeks, distance_m)

    start_km = max(base.get("week_km") or 0, runs * 4.0)
    peak_km = max(start_km, PEAK_KM[distance_m] * FREQ_FACTOR[runs])
    peak_long = PEAK_LONG_KM[distance_m]
    share = LONG_SHARE[runs] + (0.10 if distance_m == 42195 else 0)
    prev_long = max(base.get("long_km") or 0, 5.0)

    weeks = []
    volume = start_km
    top_volume = start_km
    top_long = prev_long
    build_week = 0
    phase_counts = {}
    for i, phase in enumerate(phases):
        ws = first_monday + timedelta(weeks=i)
        phase_week = phase_counts.get(phase, 0)
        phase_counts[phase] = phase_week + 1
        recovery = phase in ("base", "build") and i % 4 == 3 and i > 0

        if phase in ("base", "build", "peak"):
            if i > 0 and not recovery:
                volume = min(volume * WEEKLY_GROWTH, peak_km) if phase != "peak" else max(volume, top_volume)
            target = volume * 0.8 if recovery else volume
            top_volume = max(top_volume, volume)
            long_km = min(peak_long, target * share, prev_long + LONG_STEP_KM)
            if recovery:
                long_km = min(long_km, prev_long * 0.8)
            else:
                prev_long = max(prev_long, long_km)
            top_long = max(top_long, long_km)
        elif phase == "taper":
            n_taper = phases.count("taper")
            left = n_taper - phase_week  # 2, 1 for a two-week taper
            target = top_volume * (0.7 if n_taper == 1 else 0.75 if left == 2 else 0.55)
            long_km = top_long * (0.75 if left == 2 else 0.6)
        else:
            target = long_km = 0

        if phase == "race":
            sessions = _race_week(ws, race_day, runs, distance_m, paces, cfg.get("race_name", ""))
        else:
            sessions = []
            slots = []
            for offset, kind in DAY_TEMPLATE[runs]:
                day = ws + timedelta(days=(long_day + offset) % 7)
                slots.append((day, kind))
            slots.sort()
            fixed = []
            easy_days = []
            for day, kind in slots:
                if kind == "long":
                    fixed.append(_long(day, long_km, phase, phase_week, distance_m, paces))
                elif kind in ("q1", "q2"):
                    make = _q1 if kind == "q1" else _q2
                    s = make(day, phase, phase_week, build_week, distance_m, paces, recovery)
                    if s:
                        fixed.append(s)
                    else:  # no workout this week: an easy run, with strides in place of q1
                        easy_days.append((day, kind == "q1"))
                else:
                    easy_days.append((day, False))
            # workouts have a fixed size: in a small week they would push the total
            # past the target, so the later one, then the other, becomes an easy run
            workouts = [s for s in fixed if s["type"] != "long"]
            while workouts and (sum(s["distance_km"] for s in fixed)
                                + MIN_EASY_KM * len(easy_days) > target):
                dropped = workouts.pop()
                fixed.remove(dropped)
                easy_days.append((date.fromisoformat(dropped["date"]), not workouts))
            left_km = target - sum(s["distance_km"] for s in fixed)
            each = max(MIN_EASY_KM, left_km / len(easy_days)) if easy_days else 0
            each = math.floor(min(each, max(long_km * 0.8, MIN_EASY_KM)) * 2) / 2
            # easy runs are capped relative to the long run; what doesn't fit goes on the long run
            spill = left_km - each * len(easy_days)
            if spill >= 1:
                for s in fixed:
                    if s["type"] == "long":
                        s["distance_km"] = _round_half(min(peak_long, s["distance_km"] + spill))
            sessions = fixed + [_easy(d, each, paces, strides) for d, strides in easy_days]
            sessions = [s for s in sessions if date.fromisoformat(s["date"]) >= start]
            sessions.sort(key=lambda s: s["date"])
        if phase == "build" and not recovery:
            build_week += 1

        weeks.append({
            "index": i + 1,
            "start": ws.isoformat(),
            "phase": phase,
            "recovery": recovery,
            "target_km": round(target, 1),
            "planned_km": round(sum(s["distance_km"] for s in sessions), 1),
            "sessions": sessions,
        })

    return {
        "weeks": weeks,
        "paces": paces,
        "vdot_current": round(vdot_current, 1) if vdot_current else None,
        "vdot_goal": round(vdot_goal, 1) if vdot_goal else None,
        "vdot_train": round(vdot_train, 1),
        "predicted_time_s": round(race_time(distance_m, vdot_current)) if vdot_current else None,
    }
