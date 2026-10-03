"""SQLite storage. One short-lived connection per unit of work; WAL lets the
background scanner and web requests coexist."""
import json
import sqlite3
import zlib
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id               INTEGER PRIMARY KEY,
    file_hash        TEXT UNIQUE NOT NULL,
    file_name        TEXT,
    stored_path      TEXT,
    source_path      TEXT,
    name             TEXT,
    sport            TEXT,
    sub_sport        TEXT,
    is_run           INTEGER NOT NULL DEFAULT 0,
    start_utc        TEXT,
    start_local      TEXT,
    local_date       TEXT,
    distance_m       REAL,
    moving_s         REAL,
    elapsed_s        REAL,
    avg_speed        REAL,
    max_speed        REAL,
    avg_hr           REAL,
    max_hr           REAL,
    avg_cadence      REAL,
    avg_power        REAL,
    ascent_m         REAL,
    descent_m        REAL,
    calories         REAL,
    training_effect  REAL,
    anaerobic_te     REAL,
    vo2max           REAL,
    trimp            REAL,
    trimp_estimated  INTEGER DEFAULT 0,
    efficiency       REAL,
    hr_zones         TEXT,
    splits           TEXT,
    laps             TEXT,
    polyline         TEXT,
    device           TEXT,
    strava_status    TEXT NOT NULL DEFAULT 'none',
    strava_upload_id INTEGER,
    strava_activity_id INTEGER,
    strava_error     TEXT,
    imported_at      TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_act_date ON activities(local_date);
CREATE UNIQUE INDEX IF NOT EXISTS idx_act_start ON activities(start_utc, sport);

CREATE TABLE IF NOT EXISTS streams (
    activity_id INTEGER PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
    data        BLOB
);

CREATE TABLE IF NOT EXISTS best_efforts (
    activity_id INTEGER REFERENCES activities(id) ON DELETE CASCADE,
    distance_m  REAL,
    time_s      REAL,
    start_s     REAL,
    PRIMARY KEY (activity_id, distance_m)
);

CREATE TABLE IF NOT EXISTS seen_files (
    path      TEXT PRIMARY KEY,
    size      INTEGER,
    mtime     REAL,
    file_hash TEXT,
    status    TEXT,
    message   TEXT,
    seen_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    settings.fit_store.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def kv_get(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key: str, value) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO kv(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


def kv_delete(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM kv WHERE key = ?", (key,))


def pack_streams(streams: dict) -> bytes:
    return zlib.compress(json.dumps(streams, separators=(",", ":")).encode())


def unpack_streams(blob: bytes | None) -> dict:
    return json.loads(zlib.decompress(blob)) if blob else {}
