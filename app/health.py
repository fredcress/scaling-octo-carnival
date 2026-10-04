"""Daily health metrics from Gadgetbridge's database export (Garmin watches).

Syncthing replaces the export file whenever the phone exports, so it is never
read in place: it is copied first, then every day it covers is recomputed and
upserted into health_daily. Days that later drop out of the export are kept.
"""
import logging
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import settings
from .db import connect, kv_get, kv_set

log = logging.getLogger(__name__)

SETTLE_S = 15  # Syncthing may still be writing a freshly changed file
IMPORT_VERSION = 2  # bump when compute() changes so an unchanged export is re-read
SLEEP_GAP_S = 6 * 3600  # a longer pause between stage samples starts a new night
MIN_NIGHT_S = 2 * 3600
# Garmin sleep levels as stored by Gadgetbridge
STAGES = {1: "awake", 2: "light", 3: "deep", 4: "rem"}
EVENT_SLEEP = 74  # GARMIN_EVENT_SAMPLE: type 0 = fell asleep, 1 = woke up
# GENERIC_METRIC_SAMPLE types written by Gadgetbridge for Garmin
METRIC_VO2MAX = 12
METRIC_VO2MAX_INT = 6
METRIC_PREDICTIONS = {19: 5000, 20: 10000, 21: 21097.5, 22: 42195}

COLUMNS = ("steps", "resting_hr", "sleep_start", "sleep_end", "sleep_s", "deep_s", "light_s",
           "rem_s", "awake_s", "sleep_score", "hrv_night", "hrv_weekly", "hrv_low", "hrv_high",
           "hrv_status")


def _seconds(ts: int) -> float:
    """Gadgetbridge stores some tables in seconds and others in milliseconds."""
    return ts / 1000 if ts > 1e11 else ts


def _has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def _daily_steps(conn, local_date) -> dict:
    """The watch reports a running total that resets once a day (at an hour that moves
    with the watch's time zone), so add up the increases instead of taking a maximum."""
    out = defaultdict(int)
    prev = None
    for ts, steps in conn.execute("SELECT TIMESTAMP, STEPS FROM GARMIN_ACTIVITY_SAMPLE "
                                  "WHERE STEPS >= 0 ORDER BY TIMESTAMP"):
        inc = steps - prev if prev is not None and steps >= prev else steps
        out[local_date(ts)] += inc
        prev = steps
    return out


def _sleep_windows(conn) -> list[tuple[float, float]]:
    """(fell asleep, woke up) from the watch's sleep start/stop events."""
    if not _has_table(conn, "GARMIN_EVENT_SAMPLE"):
        return []
    out, start = [], None
    for ts, kind in conn.execute("SELECT TIMESTAMP, EVENT_TYPE FROM GARMIN_EVENT_SAMPLE "
                                 "WHERE EVENT = ? AND EVENT_TYPE IN (0, 1) ORDER BY TIMESTAMP",
                                 (EVENT_SLEEP,)):
        if kind == 0:
            start = _seconds(ts)
        elif start is not None:
            out.append((start, _seconds(ts)))
            start = None
    return out


def _nights(conn, local_dt) -> dict:
    """One sleep summary per wake-up date.

    A stage sample marks the *end* of a stretch in that stage, which starts at the
    previous sample (or at the sleep-start event). This matches the watch's own totals."""
    rows = [(_seconds(t), s) for t, s in conn.execute(
        "SELECT TIMESTAMP, STAGE FROM GARMIN_SLEEP_STAGE_SAMPLE ORDER BY TIMESTAMP")]
    windows = _sleep_windows(conn)
    if windows:
        sessions = [(start, [r for r in rows if start < r[0] <= end + 60]) for start, end in windows]
    else:
        # no start events: split on long gaps; the first stretch of each night is lost
        sessions, cur = [], []
        for row in rows:
            if cur and row[0] - cur[-1][0] > SLEEP_GAP_S:
                sessions.append((cur[0][0], cur))
                cur = []
            cur.append(row)
        if cur:
            sessions.append((cur[0][0], cur))

    out = {}
    for start, s in sessions:
        if not s:
            continue
        totals = defaultdict(float)
        prev = start
        for t, stage in s:
            totals[STAGES.get(stage, "awake")] += t - prev
            prev = t
        asleep = totals["light"] + totals["deep"] + totals["rem"]
        if asleep < MIN_NIGHT_S:
            continue
        end = local_dt(s[-1][0])
        night = {"sleep_start": local_dt(start).isoformat(timespec="minutes"),
                 "sleep_end": end.isoformat(timespec="minutes"), "sleep_s": round(asleep),
                 **{f"{k}_s": round(totals[k]) for k in ("deep", "light", "rem", "awake")}}
        day = end.date().isoformat()
        if day not in out or night["sleep_s"] > out[day]["sleep_s"]:
            out[day] = night
    return out


def _latest_per_day(conn, sql: str, local_date) -> dict:
    """Last row of each local day (rows must come ordered by timestamp, first column)."""
    out = {}
    for row in conn.execute(sql):
        out[local_date(_seconds(row[0]))] = row[1:]
    return out


def compute(conn, tz: str) -> tuple[dict, dict]:
    """Daily rows {date: {column: value}} and the watch's own estimates."""
    zone = ZoneInfo(tz)

    def local_dt(ts):
        return datetime.fromtimestamp(ts, zone).replace(tzinfo=None)

    def local_date(ts):
        return local_dt(_seconds(ts)).date().isoformat()

    days = defaultdict(dict)
    if _has_table(conn, "GARMIN_ACTIVITY_SAMPLE"):
        for d, n in _daily_steps(conn, local_date).items():
            days[d]["steps"] = n
    if _has_table(conn, "GARMIN_HEART_RATE_RESTING_SAMPLE"):
        # the watch refines the day's resting HR as it goes: keep the day's last value
        for d, (hr,) in _latest_per_day(conn, "SELECT TIMESTAMP, HEART_RATE FROM "
                                        "GARMIN_HEART_RATE_RESTING_SAMPLE WHERE HEART_RATE > 0 "
                                        "ORDER BY TIMESTAMP", local_date).items():
            days[d]["resting_hr"] = hr
    if _has_table(conn, "GARMIN_SLEEP_STAGE_SAMPLE"):
        for d, night in _nights(conn, local_dt).items():
            days[d].update(night)
    if _has_table(conn, "GARMIN_SLEEP_STATS_SAMPLE"):
        for d, (score,) in _latest_per_day(conn, "SELECT TIMESTAMP, SLEEP_SCORE FROM "
                                           "GARMIN_SLEEP_STATS_SAMPLE ORDER BY TIMESTAMP",
                                           local_date).items():
            days[d]["sleep_score"] = score
    if _has_table(conn, "GARMIN_HRV_SUMMARY_SAMPLE"):
        for d, r in _latest_per_day(conn, "SELECT TIMESTAMP, LAST_NIGHT_AVERAGE, WEEKLY_AVERAGE, "
                                    "BASELINE_BALANCED_LOWER, BASELINE_BALANCED_UPPER, STATUS_NUM "
                                    "FROM GARMIN_HRV_SUMMARY_SAMPLE ORDER BY TIMESTAMP",
                                    local_date).items():
            days[d].update(zip(("hrv_night", "hrv_weekly", "hrv_low", "hrv_high", "hrv_status"), r))

    watch = {}
    if _has_table(conn, "GENERIC_METRIC_SAMPLE"):
        latest = {}
        for ts, kind, score in conn.execute(
                "SELECT TIMESTAMP, METRIC_TYPE, METRIC_SCORE FROM GENERIC_METRIC_SAMPLE "
                "WHERE METRIC_SCORE > 0 ORDER BY TIMESTAMP"):
            latest[kind] = (local_date(ts), score)
        vo2 = latest.get(METRIC_VO2MAX) or latest.get(METRIC_VO2MAX_INT)
        if vo2:
            watch["vo2max"] = {"value": round(vo2[1], 1), "date": vo2[0]}
        preds = {str(dist): {"time_s": round(latest[k][1]), "date": latest[k][0]}
                 for k, dist in METRIC_PREDICTIONS.items() if k in latest}
        if preds:
            watch["predictions"] = preds
    return dict(days), watch


def find_export() -> Path | None:
    """Newest .db file in the Gadgetbridge folder (the export's name is user-chosen)."""
    root = settings.gadgetbridge_dir
    if not root.is_dir():
        return None
    try:
        files = [p for p in root.rglob("*.db") if p.is_file() and ".stversions" not in p.parts]
    except OSError:
        return None
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def import_once(force: bool = False) -> dict | None:
    """Re-import the export if it changed since last time. Returns the import status."""
    src = find_export()
    if not src:
        return None
    st = src.stat()
    last = kv_get("health_import") or {}
    unchanged = last.get("source") == str(src) and last.get("mtime") == st.st_mtime \
        and last.get("size") == st.st_size and last.get("version") == IMPORT_VERSION
    if (unchanged and not force) or time.time() - st.st_mtime < SETTLE_S:
        return last or None

    copy = settings.data_dir / "gadgetbridge-import.db"
    shutil.copyfile(src, copy)
    try:
        with sqlite3.connect(f"file:{copy}?mode=ro", uri=True) as gb:
            days, watch = compute(gb, settings.tz)
    finally:
        copy.unlink(missing_ok=True)

    with connect() as conn:
        for day, row in days.items():
            cols = [c for c in COLUMNS if c in row]
            conn.execute(
                f"INSERT INTO health_daily(date, {', '.join(cols)}) VALUES(?{', ?' * len(cols)}) "
                f"ON CONFLICT(date) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols)}",
                [day, *(row[c] for c in cols)])
    if watch:
        kv_set("watch_metrics", watch)
    status = {"source": str(src), "mtime": st.st_mtime, "size": st.st_size, "version": IMPORT_VERSION,
              "exported_at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
              "imported_at": datetime.now(timezone.utc).isoformat(),
              "days": len(days), "first": min(days, default=None), "last": max(days, default=None)}
    kv_set("health_import", status)
    log.info("health: imported %d days from %s", len(days), src.name)
    return status
