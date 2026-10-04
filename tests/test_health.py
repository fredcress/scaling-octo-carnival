"""Health import from a small hand-built Gadgetbridge database."""
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import health, queries

TZ = ZoneInfo("Europe/Paris")


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def make_export(path, nights: dict[str, list[tuple[str, int]]], steps: list[tuple[datetime, int]],
                hrv: dict[str, tuple] = None, rhr: dict[str, list[int]] = None, events: bool = True):
    """nights: {wake-up date: [(fell asleep, None), (hh:mm, stage that ENDS here), ...]}"""
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE GARMIN_ACTIVITY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            RAW_INTENSITY INTEGER, STEPS INTEGER, RAW_KIND INTEGER, HEART_RATE INTEGER,
            DISTANCE_CM INTEGER, ACTIVE_CALORIES INTEGER);
        CREATE TABLE GARMIN_SLEEP_STAGE_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, STAGE INTEGER);
        CREATE TABLE GARMIN_SLEEP_STATS_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, SLEEP_SCORE INTEGER);
        CREATE TABLE GARMIN_HRV_SUMMARY_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            WEEKLY_AVERAGE INTEGER, LAST_NIGHT_AVERAGE INTEGER, LAST_NIGHT5_MIN_HIGH INTEGER,
            BASELINE_LOW_UPPER INTEGER, BASELINE_BALANCED_LOWER INTEGER, BASELINE_BALANCED_UPPER INTEGER,
            STATUS_NUM INTEGER);
        CREATE TABLE GARMIN_HEART_RATE_RESTING_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER, HEART_RATE INTEGER);
        CREATE TABLE GENERIC_METRIC_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            METRIC_TYPE INTEGER, METRIC_SCORE REAL, METRIC_EXTRA INTEGER);
        CREATE TABLE GARMIN_EVENT_SAMPLE (TIMESTAMP INTEGER, DEVICE_ID INTEGER, USER_ID INTEGER,
            EVENT INTEGER, EVENT_TYPE INTEGER, DATA INTEGER);
    """)
    for t, n in steps:  # activity samples are in seconds, the rest in milliseconds
        c.execute("INSERT INTO GARMIN_ACTIVITY_SAMPLE(TIMESTAMP, STEPS) VALUES(?, ?)", (int(t.timestamp()), n))
    for day, stages in nights.items():
        def at(hhmm):
            d = datetime.fromisoformat(f"{day}T{hhmm}").replace(tzinfo=TZ)
            return d - timedelta(days=1) if hhmm > "12:00" else d  # evening before the wake-up day
        for hhmm, stage in stages[1:]:
            c.execute("INSERT INTO GARMIN_SLEEP_STAGE_SAMPLE(TIMESTAMP, STAGE) VALUES(?, ?)", (ms(at(hhmm)), stage))
        start, end = at(stages[0][0]), at(stages[-1][0])
        if events:
            c.executemany("INSERT INTO GARMIN_EVENT_SAMPLE(TIMESTAMP, EVENT, EVENT_TYPE) VALUES(?, 74, ?)",
                          [(ms(start), 0), (ms(end), 1)])
        c.execute("INSERT INTO GARMIN_SLEEP_STATS_SAMPLE(TIMESTAMP, SLEEP_SCORE) VALUES(?, 75)", (ms(end),))
    for day, (night, weekly, lo, hi) in (hrv or {}).items():
        t = datetime.fromisoformat(f"{day}T07:00").replace(tzinfo=TZ)
        c.execute("INSERT INTO GARMIN_HRV_SUMMARY_SAMPLE(TIMESTAMP, LAST_NIGHT_AVERAGE, WEEKLY_AVERAGE, "
                  "BASELINE_BALANCED_LOWER, BASELINE_BALANCED_UPPER, STATUS_NUM) VALUES(?, ?, ?, ?, ?, 4)",
                  (ms(t), night, weekly, lo, hi))
    for day, values in (rhr or {}).items():
        for i, v in enumerate(values):
            t = datetime.fromisoformat(f"{day}T08:00").replace(tzinfo=TZ) + timedelta(hours=i)
            c.execute("INSERT INTO GARMIN_HEART_RATE_RESTING_SAMPLE(TIMESTAMP, HEART_RATE) VALUES(?, ?)", (ms(t), v))
    t = datetime(2026, 10, 3, 9, tzinfo=TZ)
    for kind, score in ((12, 50.34), (21, 5927), (19, 1229)):
        c.execute("INSERT INTO GENERIC_METRIC_SAMPLE(TIMESTAMP, METRIC_TYPE, METRIC_SCORE) VALUES(?, ?, ?)",
                  (ms(t), kind, score))
    c.commit()
    c.close()


def test_compute(tmp_path):
    db = tmp_path / "Gadgetbridge.db"
    d = lambda s: datetime.fromisoformat(s).replace(tzinfo=TZ)  # noqa: E731
    steps = [
        (d("2026-10-02T10:00"), 1000), (d("2026-10-02T12:00"), -1),  # -1 = no reading
        (d("2026-10-02T18:00"), 5000),
        (d("2026-10-03T00:30"), 40),  # the watch reset its total
        (d("2026-10-03T09:00"), 3040),
    ]
    # asleep at 23:00; light until 01:00, deep until 02:00, REM until 03:00, awake until 03:30, light until 07:00
    nights = {"2026-10-03": [("23:00", None), ("01:00", 2), ("02:00", 3), ("03:00", 4), ("03:30", 1), ("07:00", 2)]}
    make_export(db, nights, steps, hrv={"2026-10-03": (70, 80, 75, 95)}, rhr={"2026-10-03": [52, 49]})
    with sqlite3.connect(db) as c:
        days, watch = health.compute(c, "Europe/Paris")
    assert days["2026-10-02"]["steps"] == 5000
    assert days["2026-10-03"]["steps"] == 3040
    n = days["2026-10-03"]
    assert n["sleep_start"] == "2026-10-02T23:00" and n["sleep_end"] == "2026-10-03T07:00"
    assert (n["light_s"], n["deep_s"], n["rem_s"], n["awake_s"]) == (2 * 3600 + 3.5 * 3600, 3600, 3600, 1800)
    assert n["sleep_s"] == 7.5 * 3600 and n["sleep_score"] == 75
    assert (n["hrv_night"], n["hrv_low"], n["hrv_high"]) == (70, 75, 95)
    assert n["resting_hr"] == 49  # the day's last value
    assert watch["vo2max"]["value"] == 50.3
    assert watch["predictions"]["21097.5"]["time_s"] == 5927


def test_nights_without_sleep_events(tmp_path):
    """Older exports without start/stop events: the stretch before the first sample is lost."""
    db = tmp_path / "g.db"
    make_export(db, {"2026-10-03": [("23:00", None), ("01:00", 2), ("02:00", 3), ("03:00", 4),
                                    ("03:30", 1), ("07:00", 2)]}, [], events=False)
    with sqlite3.connect(db) as c:
        n = health.compute(c, "Europe/Paris")[0]["2026-10-03"]
    assert n["sleep_start"] == "2026-10-03T01:00"
    assert (n["light_s"], n["deep_s"], n["rem_s"], n["awake_s"]) == (3.5 * 3600, 3600, 3600, 1800)


def test_short_naps_are_not_nights(tmp_path):
    db = tmp_path / "g.db"
    make_export(db, {"2026-10-03": [("14:00", 2), ("14:40", 1)]}, [])
    with sqlite3.connect(db) as c:
        days, _ = health.compute(c, "Europe/Paris")
    assert "sleep_s" not in days.get("2026-10-03", {})


@pytest.fixture
def health_rows(monkeypatch):
    today = datetime(2026, 10, 4).date()
    monkeypatch.setattr(queries, "today", lambda: today)

    def rows(**last):
        out = [{"date": (today - timedelta(days=i)).isoformat(), "resting_hr": 48, "sleep_s": 7 * 3600,
                "sleep_score": 80, "hrv_night": 90, "hrv_low": 80, "hrv_high": 100} for i in range(10, 0, -1)]
        out.append({"date": today.isoformat(), "resting_hr": 48, "sleep_s": 7 * 3600, "sleep_score": 80,
                    "hrv_night": 90, "hrv_low": 80, "hrv_high": 100} | last)
        return out
    return rows


def test_readiness_levels(health_rows):
    assert queries.readiness(health_rows())["level"] == "good"
    r = queries.readiness(health_rows(hrv_night=70))
    assert r["level"] == "ok" and "below your usual range" in r["flags"][0]
    r = queries.readiness(health_rows(hrv_night=70, sleep_s=5 * 3600, resting_hr=55))
    assert r["level"] == "low" and len(r["flags"]) == 3
    assert queries.readiness(health_rows()[:-1]) is None  # nothing for last night yet


def test_import_once_keeps_history(tmp_path, monkeypatch):
    import dataclasses
    import os

    from app.db import connect, init_db
    gb = tmp_path / "gb"
    gb.mkdir()
    monkeypatch.setattr(health, "settings", dataclasses.replace(health.settings, gadgetbridge_dir=gb))
    init_db()
    src = gb / "Gadgetbridge.db"
    make_export(src, {"2026-09-01": [("23:00", 2), ("02:00", 3), ("04:00", 2), ("06:00", 1)]}, [])
    old = datetime.now().timestamp() - 60
    os.utime(src, (old, old))
    status = health.import_once()
    assert status["days"] >= 1
    assert health.import_once() == status  # unchanged file: no re-import
    src.unlink()
    make_export(src, {"2026-09-02": [("23:00", 2), ("02:00", 3), ("04:00", 2), ("06:00", 1)]}, [])
    os.utime(src, (old + 1, old + 1))
    health.import_once()
    with connect() as c:
        dates = {r[0] for r in c.execute("SELECT date FROM health_daily WHERE sleep_s IS NOT NULL")}
    assert {"2026-09-01", "2026-09-02"} <= dates  # the night no longer in the export is kept
