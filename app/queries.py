"""Read models for the API. KPIs only ever count runs (is_run = 1)."""
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import analytics as an
from . import plan
from .config import settings
from .db import connect, unpack_streams

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
        "vo2max": {"value": vo2[-1]["vo2max"], "date": vo2[-1]["local_date"]} if vo2 else None,
        "weeks": weeks,
        "fitness": fitness,
        "current": fitness[-1] if fitness else None,
        "recent": recent,
        "unsent": unsent,
        "total_activities": total_acts,
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
    first = weeks[0]["start"]
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, name, local_date, distance_m FROM activities "
            "WHERE is_run = 1 AND local_date BETWEEN ? AND ? ORDER BY start_utc",
            (first, cfg["race_date"])).fetchall()
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["local_date"]].append({"id": r["id"], "name": r["name"],
                                        "distance_km": round((r["distance_m"] or 0) / 1000, 2)})
    current = None
    for w in weeks:
        ws = date.fromisoformat(w["start"])
        days = {(ws + timedelta(days=i)).isoformat() for i in range(7)}
        planned_days = set()
        for s in w["sessions"]:
            planned_days.add(s["date"])
            s["runs"] = by_day.get(s["date"], [])
            done_km = sum(r["distance_km"] for r in s["runs"])
            d = date.fromisoformat(s["date"])
            if done_km >= 0.6 * s["distance_km"]:
                s["status"] = "done"
            elif d > t:
                s["status"] = "upcoming"
            elif d == t:
                s["status"] = "today"
            else:
                s["status"] = "partial" if done_km else "missed"
        w["extra_runs"] = [dict(r, date=day) for day in sorted(days - planned_days)
                           for r in by_day.get(day, [])]
        w["actual_km"] = round(sum(r["distance_km"] for day in days for r in by_day.get(day, [])), 1)
        if ws <= t < ws + timedelta(days=7):
            current = w["index"]
    race_day = date.fromisoformat(cfg["race_date"])
    return {
        **gen,
        "config": {k: cfg[k] for k in ("race_name", "race_date", "distance_m", "goal_time_s",
                                       "runs_per_week", "long_run_day", "start_date")},
        "label": plan.RACE_DISTANCES[cfg["distance_m"]],
        "baseline": cfg["baseline"],
        "today": t.isoformat(),
        "days_to_race": (race_day - t).days,
        "current_week": current,
        "finished": t > race_day,
    }


def heatmap() -> list:
    with connect() as conn:
        return [json.loads(r["polyline"]) for r in conn.execute(
            "SELECT polyline FROM activities WHERE is_run = 1 AND polyline IS NOT NULL")]


def import_log(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT path, status, message, seen_at FROM seen_files "
            "ORDER BY seen_at DESC, rowid DESC LIMIT ?", (limit,))]
