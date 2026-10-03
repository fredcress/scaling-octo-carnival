import json

from app.db import connect
from app.fitimport import import_file


def test_scan_counts(imported):
    assert imported["imported"] == 3
    assert imported["error"] == 1


def test_run_fields(imported):
    with connect() as conn:
        a = conn.execute("SELECT * FROM activities WHERE file_name = 'run1.fit'").fetchone()
    assert a["is_run"] == 1
    assert a["sport"] == "running"
    assert 5300 < a["distance_m"] < 5500
    assert 150 < a["avg_cadence"] < 200  # converted to steps/min
    assert a["trimp"] > 0 and a["trimp_estimated"] == 0
    assert len(json.loads(a["hr_zones"])) == 5
    assert len(json.loads(a["splits"])) == 6
    assert json.loads(a["polyline"])
    # local time comes from the FIT activity message (+2 h in the generator)
    assert a["start_local"] != a["start_utc"][:19]


def test_trail_and_walk(imported):
    with connect() as conn:
        rows = {r["file_name"]: r for r in conn.execute("SELECT * FROM activities")}
    assert rows["run2.fit"]["name"].endswith("Trail Run")
    assert rows["walk.fit"]["is_run"] == 0
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM best_efforts WHERE activity_id = ?",
                         (rows["walk.fit"]["id"],)).fetchone()[0]
    assert n == 0  # non-runs never produce records


def test_best_efforts_stored(imported):
    with connect() as conn:
        dists = {r[0] for r in conn.execute("SELECT DISTINCT distance_m FROM best_efforts")}
    assert {1000, 1609.344, 5000} <= dists


def test_reimport_is_duplicate(imported, watch_dir):
    status, _, _ = import_file(watch_dir / "run1.fit")
    assert status == "duplicate"


def test_rescan_skips_seen_files(imported):
    from app import watcher
    assert watcher.scan_once() == {"imported": 0, "duplicate": 0, "skipped": 0, "error": 0}
