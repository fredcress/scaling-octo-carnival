"""Read models for the API. KPIs only ever count runs (is_run = 1)."""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import adapt, plan
from . import analytics as an
from .config import settings
from .db import connect, kv_get, unpack_streams

LIST_COLS = """id, name, sport, sub_sport, is_run, start_local, local_date, distance_m,
    moving_s, avg_speed, avg_hr, ascent_m, trimp, trimp_estimated,
    strava_status, strava_activity_id, strava_error"""


def today() -> date:
    return datetime.now(ZoneInfo(settings.tz)).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _runs(conn, cols="*"):
    return conn.execute(f"SELECT {cols} FROM activities WHERE is_run = 1 ORDER BY start_utc").fetchall()


def _totals(rows) -> dict:
    dist = sum(r["distance_m"] or 0 for r in rows)
    secs = sum(r["moving_s"] or 0 for r in rows)
    return {"runs": len(rows), "distance_m": dist, "moving_s": secs,
            "pace_s_per_km": secs / dist * 1000 if dist else None}


def _pace_window(rows, start: date, end: date):
    sel = [r for r in rows if start <= date.fromisoformat(r["local_date"]) < end]
    return _totals(sel)["pace_s_per_km"]


def summary() -> dict:
    t = today()
    wk = week_start(t)
    with connect() as conn:
        runs = _runs(conn, "id, local_date, distance_m, moving_s, trimp, vo2max, start_utc")
        recent = [dict(r) for r in conn.execute(
            f"SELECT {LIST_COLS} FROM activities ORDER BY start_utc DESC LIMIT 8")]
        unsent = conn.execute(
            "SELECT COUNT(*) FROM activities WHERE strava_status IN ('none', 'error')").fetchone()[0]
        total_acts = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    def between(a: date, b: date):
        return [r for r in runs if a <= date.fromisoformat(r["local_date"]) < b]

    weeks = []
    for i in range(25, -1, -1):
        ws = wk - timedelta(weeks=i)
        sel = between(ws, ws + timedelta(days=7))
        weeks.append({"week": ws.isoformat(), **_totals(sel),
                      "long_run_m": max((r["distance_m"] or 0 for r in sel), default=0)})

    fitness = []
    if runs:
        load = defaultdict(float)
        for r in runs:
            load[r["local_date"]] += r["trimp"] or 0
        first = date.fromisoformat(runs[0]["local_date"])
        series = an.fitness_series(load, first, t)
        fitness = series[-180:]

    vo2 = [r for r in runs if r["vo2max"]]
    # Garmin's FIT files often leave VO2max out; the Gadgetbridge export has it
    watch_vo2 = (kv_get("watch_metrics") or {}).get("vo2max")
    month_start = t.replace(day=1)
    return {
        "today": t.isoformat(),
        "this_week": _totals(between(wk, t + timedelta(days=1))),
        "last_week": _totals(between(wk - timedelta(days=7), wk)),
        # same weekday last week, so a half-finished week compares fairly
        "last_week_to_date": _totals(between(wk - timedelta(days=7), t - timedelta(days=6))),
        "this_month": _totals(between(month_start, t + timedelta(days=1))),
        "this_year": _totals(between(t.replace(month=1, day=1), t + timedelta(days=1))),
        "all_time": _totals(runs),
        "pace_28d": _pace_window(runs, t - timedelta(days=27), t + timedelta(days=1)),
        "pace_prev_28d": _pace_window(runs, t - timedelta(days=55), t - timedelta(days=27)),
        "vo2max": ({"value": vo2[-1]["vo2max"], "date": vo2[-1]["local_date"]} if vo2
                   and (not watch_vo2 or vo2[-1]["local_date"] >= watch_vo2["date"]) else watch_vo2),
        "weeks": weeks,
        "fitness": fitness,
        "current": fitness[-1] if fitness else None,
        "recent": recent,
        "unsent": unsent,
        "total_activities": total_acts,
        "readiness": readiness(),
    }


def activities(kind: str) -> list[dict]:
    where = "WHERE is_run = 1" if kind == "run" else ""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT {LIST_COLS} FROM activities {where} ORDER BY start_utc DESC")]


def activity(activity_id: int) -> dict | None:
    with connect() as conn:
        a = conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
        if not a:
            return None
        s = conn.execute("SELECT data FROM streams WHERE activity_id = ?", (activity_id,)).fetchone()
        efforts = conn.execute(
            "SELECT distance_m, time_s, start_s FROM best_efforts WHERE activity_id = ? "
            "ORDER BY distance_m", (activity_id,)).fetchall()
        prs = {r["distance_m"]: r["best"] for r in conn.execute(
            "SELECT distance_m, MIN(time_s) AS best FROM best_efforts GROUP BY distance_m")}
    out = dict(a)
    for key in ("hr_zones", "splits", "laps", "polyline"):
        out[key] = json.loads(out[key]) if out[key] else None
    out.pop("stored_path", None)
    out["streams"] = unpack_streams(s["data"] if s else None)
    out["best_efforts"] = [
        {"distance_m": e["distance_m"], "label": an.BEST_EFFORT_DISTANCES.get(e["distance_m"]),
         "time_s": e["time_s"], "is_pr": e["time_s"] <= prs.get(e["distance_m"], 0)}
        for e in efforts]
    out["zone_limits"] = an.hr_zone_limits(settings.max_hr, settings.resting_hr)
    return out


def trends() -> dict:
    t = today()
    with connect() as conn:
        runs = _runs(conn, "id, name, local_date, distance_m, moving_s, avg_speed, avg_hr, "
                           "avg_cadence, efficiency, vo2max, hr_zones")

    months = []
    y, m = t.year, t.month
    for _ in range(24):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    months.reverse()
    by_month = defaultdict(list)
    for r in runs:
        by_month[r["local_date"][:7]].append(r)
    monthly = [{"month": mo, **_totals(by_month[mo])} for mo in months]

    wk = week_start(t)
    first_week = wk - timedelta(weeks=25)
    zones = {(first_week + timedelta(weeks=i)).isoformat(): [0] * 5 for i in range(26)}
    for r in runs:
        if not r["hr_zones"]:
            continue
        key = week_start(date.fromisoformat(r["local_date"])).isoformat()
        if key in zones:
            zones[key] = [a + b for a, b in zip(zones[key], json.loads(r["hr_zones"]))]

    points = [{
        "id": r["id"], "name": r["name"], "date": r["local_date"],
        "distance_m": r["distance_m"],
        "pace_s_per_km": r["moving_s"] / r["distance_m"] * 1000 if r["distance_m"] else None,
        "avg_hr": r["avg_hr"], "cadence": r["avg_cadence"],
        "efficiency": r["efficiency"], "vo2max": r["vo2max"],
    } for r in runs if (r["distance_m"] or 0) >= 1000]

    return {"monthly": monthly,
            "weekly_zones": [{"week": k, "seconds": v} for k, v in zones.items()],
            "zone_limits": an.hr_zone_limits(settings.max_hr, settings.resting_hr),
            "runs": points}


def records() -> dict:
    t = today()
    with connect() as conn:
        rows = conn.execute("""
            SELECT b.distance_m, b.time_s, a.id, a.name, a.local_date
            FROM best_efforts b JOIN activities a ON a.id = b.activity_id
            ORDER BY b.distance_m, b.time_s""").fetchall()
    by_dist = defaultdict(list)
    for r in rows:
        by_dist[r["distance_m"]].append(dict(r))

    bests = [{"distance_m": d, "label": label, "top": by_dist.get(d, [])[:5]}
             for d, label in an.BEST_EFFORT_DISTANCES.items()]

    # Predictions from the last 90 days, from efforts >= 5 km when possible
    # (Riegel over-predicts long races from short efforts).
    cutoff = (t - timedelta(days=90)).isoformat()
    recent = [r for r in rows if r["local_date"] >= cutoff]
    sources = [r for r in recent if r["distance_m"] >= 5000] or recent
    predictions = []
    for target in (5000, 10000, 21097.5, 42195):
        best = None
        for s in sources:
            pred = an.riegel(s["time_s"], s["distance_m"], target)
            if best is None or pred < best["time_s"]:
                best = {"time_s": pred, "from_label": an.BEST_EFFORT_DISTANCES[s["distance_m"]],
                        "from_time_s": s["time_s"], "from_id": s["id"], "from_date": s["local_date"]}
        predictions.append({"distance_m": target, "label": an.BEST_EFFORT_DISTANCES[target],
                            **(best or {"time_s": None})})
    return {"bests": bests, "predictions": predictions, "window_days": 90}


def plan_baseline() -> dict:
    """Where training starts from: recent weekly volume, longest recent run and the
    VDOT of the best effort of the last 90 days."""
    t = today()
    with connect() as conn:
        runs = _runs(conn, "local_date, distance_m")
        efforts = conn.execute("""
            SELECT b.distance_m, b.time_s FROM best_efforts b
            JOIN activities a ON a.id = b.activity_id
            WHERE a.is_run = 1 AND a.local_date >= ?""",
            ((t - timedelta(days=90)).isoformat(),)).fetchall()
    out = {"week_km": 0.0, "long_km": 0.0, "vdot": None, "vdot_from": None}
    if runs:
        first = date.fromisoformat(runs[0]["local_date"])
        # a new user's first fortnight shouldn't be averaged over four weeks
        window = max(7, min(28, (t - first).days + 1))
        recent = [r for r in runs if (t - date.fromisoformat(r["local_date"])).days < window]
        out["week_km"] = round(sum(r["distance_m"] or 0 for r in recent) / 1000 / (window / 7), 1)
        out["long_km"] = round(max((r["distance_m"] or 0 for r in runs
                                    if (t - date.fromisoformat(r["local_date"])).days < 42),
                                   default=0) / 1000, 1)
    # same rule as the Records predictions: short efforts flatter long races
    sources = [e for e in efforts if e["distance_m"] >= 5000] or efforts
    for e in sources:
        v = plan.vdot(e["distance_m"], e["time_s"])
        if out["vdot"] is None or v > out["vdot"]:
            out["vdot"] = round(v, 1)
            out["vdot_from"] = {"label": an.BEST_EFFORT_DISTANCES[e["distance_m"]], "time_s": e["time_s"]}
    out["predictions"] = ({str(d): round(plan.race_time(d, out["vdot"])) for d in plan.RACE_DISTANCES}
                          if out["vdot"] else {})
    return out


def plan_view(cfg: dict) -> dict:
    """The generated plan with each session marked against the runs actually done."""
    t = today()
    gen = plan.generate(cfg)
    weeks = gen["weeks"]
    adapt.apply_overrides(weeks, kv_get("plan_overrides") or {}, gen["paces"])
    first = weeks[0]["start"]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, local_date, distance_m, hr_zones, splits FROM activities "
            "WHERE is_run = 1 AND local_date BETWEEN ? AND ? ORDER BY start_utc",
            (first, cfg["race_date"])).fetchall()
    runs = [{"id": r["id"], "name": r["name"], "date": r["local_date"],
             "distance_km": round((r["distance_m"] or 0) / 1000, 2),
             "hr_zones": json.loads(r["hr_zones"]) if r["hr_zones"] else None,
             "splits": json.loads(r["splits"]) if r["splits"] else None} for r in rows]
    ready = readiness()
    # match what was actually run to the plan and re-plan the rest of each week
    adapt.reconcile(weeks, runs, t, gen["paces"], ready["level"] if ready else None,
                    kv_get("run_kinds") or {})
    for r in runs:  # the page doesn't need the raw data
        r.pop("hr_zones")
        r.pop("splits")
    current = next((w["index"] for w in weeks
                    if date.fromisoformat(w["start"]) <= t < date.fromisoformat(w["start"]) + timedelta(days=7)),
                   None)
    race_day = date.fromisoformat(cfg["race_date"])
    cur = next((w for w in weeks if w["index"] == current), None)
    return {
        **gen,
        "advice": adapt.advice(cur, ready, t, weeks) if t <= race_day else [],
        "overrides": kv_get("plan_overrides") or {},
        "config": {k: cfg[k] for k in ("race_name", "race_date", "distance_m", "goal_time_s",
                                       "runs_per_week", "long_run_day", "start_date")},
        "label": plan.RACE_DISTANCES[cfg["distance_m"]],
        "baseline": cfg["baseline"],
        "today": t.isoformat(),
        "days_to_race": (race_day - t).days,
        "current_week": current,
        "finished": t > race_day,
        "readiness": ready,
        "watch": kv_get("watch_metrics"),
    }


HRV_STATUS = {1: "poor", 2: "low", 3: "unbalanced", 4: "balanced"}


def _health_rows(conn, since: date) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM health_daily WHERE date >= ? ORDER BY date", (since.isoformat(),))]


BIG_RUN_KM = 15
BIG_LOAD_FACTOR = 1.8  # times the average load of a run over the last 4 weeks


def _big_session_yesterday(t: date) -> str | None:
    """'21.1 km long run' when yesterday's running was long or unusually hard."""
    y = (t - timedelta(days=1)).isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT local_date, distance_m, trimp FROM activities WHERE is_run = 1 "
                            "AND local_date BETWEEN ? AND ?", ((t - timedelta(days=28)).isoformat(), y)).fetchall()
    yday = [r for r in rows if r["local_date"] == y]
    if not yday:
        return None
    km = sum(r["distance_m"] or 0 for r in yday) / 1000
    load = sum(r["trimp"] or 0 for r in yday)
    loads = [r["trimp"] for r in rows if r["trimp"]]
    avg = sum(loads) / len(loads) if loads else 0
    if km >= BIG_RUN_KM:
        return f"{km:.1f} km long run"
    if avg and load >= BIG_LOAD_FACTOR * avg:
        return f"hard {km:.1f} km session"
    return None


def readiness(rows: list[dict] | None = None) -> dict | None:
    """Morning check from last night's HRV and sleep and today's resting HR.
    None when there is no data for last night (the export hasn't caught up yet)."""
    t = today()
    if rows is None:
        with connect() as conn:
            rows = _health_rows(conn, t - timedelta(days=31))
    by_day = {r["date"]: r for r in rows}
    cur = by_day.get(t.isoformat())
    if not cur or (cur["sleep_s"] is None and cur["hrv_night"] is None):
        return None
    flags = []
    if cur["hrv_night"] and cur["hrv_low"] and cur["hrv_night"] < cur["hrv_low"]:
        flags.append(f"HRV {cur['hrv_night']} ms is below your usual range "
                     f"({cur['hrv_low']}–{cur['hrv_high']})")
    if cur["sleep_s"] is not None and cur["sleep_s"] < 6 * 3600:
        flags.append(f"only {cur['sleep_s'] // 3600}h{cur['sleep_s'] % 3600 // 60:02d} of sleep")
    elif cur["sleep_score"] is not None and cur["sleep_score"] < 60:
        flags.append(f"sleep score {cur['sleep_score']}")
    past = sorted(r["resting_hr"] for d, r in by_day.items() if d < t.isoformat() and r["resting_hr"])
    if cur["resting_hr"] and len(past) >= 7:
        normal = past[len(past) // 2]
        if cur["resting_hr"] >= normal + 5:
            flags.append(f"resting HR {round(cur['resting_hr'])} bpm, {round(cur['resting_hr'] - normal)} "
                         "above your usual")
    level = "good" if not flags else "ok" if len(flags) == 1 else "low"
    # a dip the morning after a big session is the training working, not a warning sign
    context = None
    big = _big_session_yesterday(t)
    if flags and big:
        context = f"Expected after yesterday's {big} – this usually settles within a day or two."
    advice = {
        "good": "Recovered: go ahead with today's session.",
        "ok": "Slightly under par: do today's session but keep easy parts truly easy.",
        "low": "Not well recovered: swap a hard session for an easy run or rest.",
    }[level]
    return {"date": t.isoformat(), "level": level, "flags": flags, "advice": advice, "context": context,
            "hrv_night": cur["hrv_night"], "sleep_s": cur["sleep_s"], "sleep_score": cur["sleep_score"],
            "resting_hr": cur["resting_hr"]}


def health(days: int = 120) -> dict:
    t = today()
    with connect() as conn:
        rows = _health_rows(conn, t - timedelta(days=days - 1))
    for r in rows:
        r["hrv_status_label"] = HRV_STATUS.get(r["hrv_status"])
    return {"today": t.isoformat(), "days": rows, "readiness": readiness(rows),
            "watch": kv_get("watch_metrics"), "import": kv_get("health_import"),
            "configured": settings.gadgetbridge_dir.is_dir()}


def heatmap() -> list:
    with connect() as conn:
        return [json.loads(r["polyline"]) for r in conn.execute(
            "SELECT polyline FROM activities WHERE is_run = 1 AND polyline IS NOT NULL")]


def import_log(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT path, status, message, seen_at FROM seen_files "
            "ORDER BY seen_at DESC, rowid DESC LIMIT ?", (limit,))]
