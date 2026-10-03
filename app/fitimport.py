"""Parse FIT activity files and store them with their derived metrics."""
import hashlib
import json
import logging
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import fitdecode

from . import analytics as an
from .config import settings
from .db import connect, pack_streams

log = logging.getLogger(__name__)

RUN_SPORTS = {"running"}
SEMICIRCLE = 180 / 2**31
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)

SPORT_LABELS = {
    "running": "Run", "cycling": "Ride", "walking": "Walk", "hiking": "Hike",
    "swimming": "Swim", "training": "Workout", "fitness_equipment": "Workout",
    "rowing": "Row", "e_biking": "E-Bike Ride", "mountaineering": "Hike",
}
SUB_SPORT_LABELS = {"trail": "Trail Run", "treadmill": "Treadmill Run",
                    "track": "Track Run", "indoor_cycling": "Indoor Ride",
                    "virtual_activity": "Virtual Ride"}


class NotAnActivity(Exception):
    pass


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _v(frame, *names):
    """First present, non-None field value among names."""
    for n in names:
        if frame.has_field(n):
            val = frame.get_value(n, fallback=None)
            if val is not None:
                return val
    return None


def _num(x):
    return float(x) if isinstance(x, (int, float)) else None


def _utc(ts):
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return FIT_EPOCH + timedelta(seconds=ts)
    return None


def parse_fit(path: Path) -> dict:
    """Read a FIT file into raw sessions, laps, records and a few extras."""
    file_type = None
    device = None
    sessions, laps, records = [], [], []
    activity_local_offset = None
    vo2max = None

    with fitdecode.FitReader(str(path), check_crc=fitdecode.CrcCheck.WARN,
                             error_handling=fitdecode.ErrorHandling.WARN) as fit:
        for frame in fit:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue
            name = frame.name
            if name == "file_id":
                file_type = _v(frame, "type")
                manu = _v(frame, "manufacturer")
                prod = _v(frame, "garmin_product", "product_name", "product")
                device = " ".join(str(x) for x in (manu, prod) if x is not None) or None
            elif name == "record":
                ts = _utc(_v(frame, "timestamp"))
                if ts is None:
                    continue
                lat, lon = _v(frame, "position_lat"), _v(frame, "position_long")
                cad = _num(_v(frame, "cadence"))
                frac = _num(_v(frame, "fractional_cadence")) or 0.0
                records.append({
                    "ts": ts,
                    "lat": lat * SEMICIRCLE if isinstance(lat, int) else None,
                    "lon": lon * SEMICIRCLE if isinstance(lon, int) else None,
                    "dist": _num(_v(frame, "distance")),
                    "speed": _num(_v(frame, "enhanced_speed", "speed")),
                    "hr": _num(_v(frame, "heart_rate")),
                    "cad": cad + frac if cad is not None else None,
                    "alt": _num(_v(frame, "enhanced_altitude", "altitude")),
                    "power": _num(_v(frame, "power")),
                })
            elif name == "session":
                sessions.append({f.name: f.value for f in frame.fields})
            elif name == "lap":
                laps.append({f.name: f.value for f in frame.fields})
            elif name == "activity":
                ts, local = _v(frame, "timestamp"), _v(frame, "local_timestamp")
                ts = _utc(ts)
                if ts and local is not None:
                    local_dt = _utc(local)  # local_timestamp uses the same epoch, no tz
                    if local_dt:
                        activity_local_offset = (local_dt - ts).total_seconds()
            elif name == "unknown_140":
                # Undocumented Garmin "physiological metrics" message: field 7 holds
                # VO2max as value * 3.5 / 65536. Best effort, ignored if implausible.
                raw = _v(frame, "unknown_7")
                if isinstance(raw, (int, float)):
                    val = raw * 3.5 / 65536
                    if 20 <= val <= 95:
                        vo2max = round(val, 1)

    if file_type not in ("activity", 4):
        raise NotAnActivity(f"FIT file type is {file_type!r}, not an activity")
    if not records and not sessions:
        raise NotAnActivity("no records or sessions in file")
    return {"device": device, "sessions": sessions, "laps": laps, "records": records,
            "local_offset": activity_local_offset, "vo2max": vo2max}


def _activity_name(label: str, local: datetime) -> str:
    h = local.hour
    part = ("Night" if h < 5 else "Morning" if h < 12 else "Lunch" if h < 14
            else "Afternoon" if h < 18 else "Evening" if h < 22 else "Night")
    return f"{part} {label}"


def build_activity(raw: dict) -> dict:
    """Turn parsed FIT data into a stored activity row + streams + best efforts."""
    recs = raw["records"]
    s = raw["sessions"][0] if raw["sessions"] else {}

    sport = str(s.get("sport") or "generic")
    sub_sport = str(s.get("sub_sport") or "generic")
    start = _utc(s.get("start_time")) or (recs[0]["ts"] if recs else None)
    if start is None:
        raise NotAnActivity("no start time")

    if raw["local_offset"] is not None and abs(raw["local_offset"]) <= 14 * 3600:
        local = (start + timedelta(seconds=raw["local_offset"])).replace(tzinfo=None)
    else:
        local = start.astimezone(ZoneInfo(settings.tz)).replace(tzinfo=None)

    t0 = recs[0]["ts"] if recs else start
    t = [(r["ts"] - t0).total_seconds() for r in recs]
    d = [r["dist"] for r in recs]
    hr = [r["hr"] for r in recs]
    alt = [r["alt"] for r in recs]

    distance = _num(s.get("total_distance")) or next((x for x in reversed(d) if x), 0.0)
    moving = _num(s.get("total_timer_time")) or (t[-1] if t else 0.0)
    elapsed = _num(s.get("total_elapsed_time")) or (t[-1] if t else moving)
    avg_speed = _num(s.get("enhanced_avg_speed")) or _num(s.get("avg_speed")) or (
        distance / moving if moving else None)
    hrs = [h for h in hr if h]
    avg_hr = _num(s.get("avg_heart_rate")) or (sum(hrs) / len(hrs) if hrs else None)

    cadence = _num(s.get("avg_running_cadence")) or _num(s.get("avg_cadence"))
    if cadence is not None:
        cadence += _num(s.get("avg_fractional_cadence")) or 0.0
        if sport == "running":
            cadence *= 2  # FIT stores strides/min; show steps/min

    limits = an.hr_zone_limits(settings.max_hr, settings.resting_hr)
    zones = an.time_in_zones(t, hr, limits)
    load = an.trimp(t, hr, settings.max_hr, settings.resting_hr, settings.sex)
    load_estimated = load is None
    if load is None:
        load = an.estimated_trimp(moving, settings.sex)

    efficiency = None
    if avg_speed and avg_hr:
        efficiency = round(avg_speed * 60 / avg_hr, 3)  # metres per heartbeat

    laps = []
    for i, lap in enumerate(raw["laps"]):
        lt = _num(lap.get("total_timer_time"))
        ld = _num(lap.get("total_distance"))
        laps.append({
            "lap": i + 1, "distance_m": ld, "time_s": lt,
            "pace_s_per_km": round(lt / ld * 1000, 1) if lt and ld else None,
            "avg_hr": _num(lap.get("avg_heart_rate")),
            "max_hr": _num(lap.get("max_heart_rate")),
        })

    gps = [(r["lat"], r["lon"]) for r in recs if r["lat"] is not None and r["lon"] is not None]
    polyline = [[round(gps[i][0], 5), round(gps[i][1], 5)] for i in an.downsample(len(gps), 600)]

    idx = an.downsample(len(recs), 1500)
    streams = {"t": [round(t[i], 1) for i in idx]}
    for key in ("dist", "speed", "hr", "cad", "alt", "power", "lat", "lon"):
        vals = [recs[i][key] for i in idx]
        if key == "cad" and sport == "running":
            vals = [v * 2 if v is not None else None for v in vals]
        if any(v is not None for v in vals):
            streams[key] = [round(v, 5 if key in ("lat", "lon") else 2) if v is not None else None
                            for v in vals]

    label = SUB_SPORT_LABELS.get(sub_sport) if sport in ("running", "cycling") else None
    label = label or SPORT_LABELS.get(sport, sport.replace("_", " ").title())

    row = {
        "name": _activity_name(label, local),
        "sport": sport, "sub_sport": sub_sport, "is_run": int(sport in RUN_SPORTS),
        "start_utc": start.isoformat(), "start_local": local.isoformat(),
        "local_date": local.date().isoformat(),
        "distance_m": distance, "moving_s": moving, "elapsed_s": elapsed,
        "avg_speed": avg_speed,
        "max_speed": _num(s.get("enhanced_max_speed")) or _num(s.get("max_speed")),
        "avg_hr": avg_hr, "max_hr": _num(s.get("max_heart_rate")) or (max(hrs) if hrs else None),
        "avg_cadence": cadence, "avg_power": _num(s.get("avg_power")),
        "ascent_m": _num(s.get("total_ascent")), "descent_m": _num(s.get("total_descent")),
        "calories": _num(s.get("total_calories")),
        "training_effect": _num(s.get("total_training_effect")),
        "anaerobic_te": _num(s.get("total_anaerobic_training_effect")),
        "vo2max": raw["vo2max"],
        "trimp": load, "trimp_estimated": int(load_estimated), "efficiency": efficiency,
        "hr_zones": json.dumps(zones) if zones else None,
        "splits": json.dumps(an.km_splits(t, d, hr, alt)),
        "laps": json.dumps(laps),
        "polyline": json.dumps(polyline) if polyline else None,
        "device": raw["device"],
    }
    efforts = an.best_efforts(t, d) if row["is_run"] else {}
    return {"row": row, "streams": streams, "efforts": efforts}


def _save(conn, activity_id: int, built: dict) -> None:
    conn.execute("INSERT OR REPLACE INTO streams(activity_id, data) VALUES(?, ?)",
                 (activity_id, pack_streams(built["streams"])))
    conn.execute("DELETE FROM best_efforts WHERE activity_id = ?", (activity_id,))
    conn.executemany(
        "INSERT INTO best_efforts(activity_id, distance_m, time_s, start_s) VALUES(?,?,?,?)",
        [(activity_id, dist, tm, st) for dist, (tm, st) in built["efforts"].items()])


def import_file(path: Path) -> tuple[str, str, int | None]:
    """Import one FIT file. Returns (status, message, activity_id)."""
    digest = file_hash(path)
    with connect() as conn:
        existing = conn.execute("SELECT id FROM activities WHERE file_hash = ?",
                                (digest,)).fetchone()
    if existing:
        return "duplicate", "already imported", existing["id"]

    try:
        built = build_activity(parse_fit(path))
    except NotAnActivity as e:
        return "skipped", str(e), None
    except Exception as e:  # corrupt or unreadable file: log it, keep scanning
        log.exception("failed to parse %s", path)
        return "error", f"{type(e).__name__}: {e}", None

    stored = settings.fit_store / f"{digest}.fit"
    row = built["row"] | {"file_hash": digest, "file_name": path.name,
                          "stored_path": str(stored), "source_path": str(path)}
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    with connect() as conn:
        clash = conn.execute("SELECT id FROM activities WHERE start_utc = ? AND sport = ?",
                             (row["start_utc"], row["sport"])).fetchone()
        if clash:
            return "duplicate", f"same start time as activity {clash['id']}", clash["id"]
        # keep our own copy so Strava uploads still work if the synced file disappears
        shutil.copy2(path, stored)
        cur = conn.execute(f"INSERT INTO activities({cols}) VALUES({marks})",
                           list(row.values()))
        _save(conn, cur.lastrowid, built)
        return "imported", row["name"], cur.lastrowid


def reprocess(activity_id: int) -> None:
    """Recompute derived metrics from the archived FIT file (e.g. after a MAX_HR change)."""
    with connect() as conn:
        a = conn.execute("SELECT stored_path FROM activities WHERE id = ?",
                         (activity_id,)).fetchone()
    built = build_activity(parse_fit(Path(a["stored_path"])))
    row = built["row"]
    sets = ", ".join(f"{k} = ?" for k in row)
    with connect() as conn:
        conn.execute(f"UPDATE activities SET {sets} WHERE id = ?",
                     [*row.values(), activity_id])
        _save(conn, activity_id, built)
