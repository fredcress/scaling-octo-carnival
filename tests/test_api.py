import pytest
from fastapi.testclient import TestClient

from app import watcher
from app.main import app

H = {"X-Requested-With": "fetch"}


@pytest.fixture(scope="module")
def client(imported, monkeypatch_module):
    monkeypatch_module.setattr(watcher, "start", lambda: None)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def authed(client):
    r = client.post("/login", data={"username": "runner", "password": "secret"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client


def test_requires_login(client):
    assert client.get("/api/summary").status_code == 401
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_bad_login(client):
    r = client.post("/login", data={"username": "runner", "password": "nope"},
                    follow_redirects=False)
    assert r.headers["location"] == "/login?error=1"


def test_summary(authed):
    s = authed.get("/api/summary").json()
    assert s["all_time"]["runs"] == 2  # the walk is stored but not counted
    assert s["total_activities"] == 3
    assert len(s["weeks"]) == 26
    assert s["current"]["ctl"] > 0
    assert s["unsent"] == 3


def test_activity_lists(authed):
    assert len(authed.get("/api/activities?kind=run").json()) == 2
    assert len(authed.get("/api/activities?kind=all").json()) == 3


def test_activity_detail(authed):
    aid = authed.get("/api/activities?kind=run").json()[0]["id"]
    a = authed.get(f"/api/activities/{aid}").json()
    assert "stored_path" not in a
    assert a["streams"]["hr"] and a["streams"]["dist"]
    assert len(a["zone_limits"]) == 5
    assert authed.get("/api/activities/99999").status_code == 404


def test_trends_records_heatmap_settings(authed):
    t = authed.get("/api/trends").json()
    assert len(t["monthly"]) == 24 and len(t["runs"]) == 2
    r = authed.get("/api/records").json()
    five_k = next(p for p in r["predictions"] if p["distance_m"] == 5000)
    assert five_k["time_s"] > 0
    assert len(authed.get("/api/heatmap").json()) == 2
    st = authed.get("/api/settings").json()
    assert st["strava"] == {"configured": False, "connected": False, "athlete": None}
    assert any(x["status"] == "error" for x in st["import_log"])


def test_post_needs_header(authed):
    aid = authed.get("/api/activities").json()[0]["id"]
    assert authed.post(f"/api/activities/{aid}/strava").status_code == 403


def test_push_without_strava(authed):
    aid = authed.get("/api/activities").json()[0]["id"]
    r = authed.post(f"/api/activities/{aid}/strava", headers=H)
    assert r.status_code == 400


def test_plan_lifecycle(authed):
    from datetime import date, timedelta
    r = authed.get("/api/plan").json()
    assert r["plan"] is None and r["baseline"]["week_km"] > 0
    race = (date.today() + timedelta(weeks=12)).isoformat()
    body = {"race_name": "Semi", "race_date": race, "distance_m": 21097.5,
            "goal_time_s": 6600, "runs_per_week": 4, "long_run_day": 6}
    assert authed.post("/api/plan", json=body).status_code == 403  # no X-Requested-With
    p = authed.post("/api/plan", json=body, headers=H).json()["plan"]
    assert p["config"]["race_name"] == "Semi" and p["weeks"][-1]["phase"] == "race"
    assert p["current_week"] in (None, 1)
    statuses = {s["status"] for w in p["weeks"] for s in w["sessions"]}
    assert statuses <= {"done", "partial", "missed", "today", "upcoming"}
    assert authed.get("/api/plan").json()["plan"]["config"]["race_date"] == race

    for bad in ({"race_date": date.today().isoformat()}, {"distance_m": 15000},
                {"goal_time_s": 600}, {"runs_per_week": 7}):
        assert authed.post("/api/plan", json=body | bad, headers=H).status_code in (400, 422), bad

    assert authed.delete("/api/plan").status_code == 403
    assert authed.delete("/api/plan", headers=H).json()["plan"] is None


def test_scan_endpoint(authed):
    assert authed.post("/api/scan", headers=H).json()["imported"] == 0
