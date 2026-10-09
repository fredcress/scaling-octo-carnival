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


def test_fit_download(authed):
    aid = authed.get("/api/activities?kind=run").json()[0]["id"]
    r = authed.get(f"/api/activities/{aid}/fit")
    assert r.status_code == 200 and r.content[8:12] == b".FIT"
    assert ".fit" in r.headers["content-disposition"]
    assert authed.get("/api/activities/99999/fit").status_code == 404


def test_mark_on_strava_by_hand(authed):
    aid = authed.get("/api/activities").json()[0]["id"]
    before = authed.get("/api/summary").json()["unsent"]
    url = f"/api/activities/{aid}/strava/manual"
    assert authed.post(url).status_code == 403  # no X-Requested-With
    assert authed.post(url, headers=H).json() == {"strava_status": "manual"}
    assert authed.get(f"/api/activities/{aid}/strava").json()["strava_status"] == "manual"
    assert authed.get("/api/summary").json()["unsent"] == before - 1
    assert authed.delete(url, headers=H).json() == {"strava_status": "none"}
    assert authed.delete(url, headers=H).status_code == 409
    assert authed.get("/api/summary").json()["unsent"] == before


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

    # adjust: move a week-2 session onto another day of that week, then undo
    wk = p["weeks"][1]
    first = wk["sessions"][0]
    other_day = next(d for d in (date.fromisoformat(wk["start"]) + timedelta(days=i) for i in range(7))
                     if d.isoformat() != first["date"]).isoformat()
    r = authed.post("/api/plan/adjust", headers=H,
                    json={"session": first["id"], "action": "move", "to": other_day}).json()
    moved = next(s for w in r["plan"]["weeks"] for s in w["sessions"] if s["id"] == first["id"])
    assert moved["date"] == other_day and moved["moved_from"] == first["date"]
    r = authed.post("/api/plan/adjust", headers=H, json={"session": first["id"], "action": "reset"}).json()
    assert r["plan"]["overrides"] == {}
    r = authed.post("/api/plan/adjust", headers=H, json={"session": first["id"], "action": "skip"}).json()
    assert next(s for w in r["plan"]["weeks"] for s in w["sessions"] if s["id"] == first["id"])["status"] == "skipped"
    assert authed.post("/api/plan/adjust", headers=H,
                       json={"session": first["id"], "action": "move", "to": "2020-01-01"}).status_code == 400
    assert authed.post("/api/plan", json=body, headers=H).json()["plan"]["overrides"] == {}  # rebuild clears

    aid = authed.get("/api/activities?kind=run").json()[0]["id"]
    assert authed.post("/api/plan/run-kind", headers=H, json={"activity_id": aid, "kind": "fast"}).status_code == 400
    assert authed.post("/api/plan/run-kind", headers=H, json={"activity_id": aid, "kind": "long"}).status_code == 200
    from app.db import kv_get
    assert kv_get("run_kinds") == {str(aid): "long"}
    authed.post("/api/plan/run-kind", headers=H, json={"activity_id": aid, "kind": None})
    assert kv_get("run_kinds") == {}

    assert authed.delete("/api/plan").status_code == 403
    assert authed.delete("/api/plan", headers=H).json()["plan"] is None


def test_scan_endpoint(authed):
    assert authed.post("/api/scan", headers=H).json()["imported"] == 0


def test_installable_as_app(client):
    m = client.get("/static/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")  # served from the root, without a login, so it controls the whole app
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]


@pytest.fixture
def kiosk(imported, monkeypatch):
    import dataclasses
    from app import main
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, kiosk_token="wall-token"))
    return TestClient(app)  # no session cookie


def test_kiosk_token(kiosk):
    r = kiosk.get("/kiosk?token=wall-token")
    assert r.status_code == 200 and r.headers["referrer-policy"] == "no-referrer"
    assert kiosk.get("/kiosk?token=nope", follow_redirects=False).headers["location"] == "/login"
    assert kiosk.get("/kiosk", follow_redirects=False).headers["location"] == "/login"

    T = {"X-Kiosk-Token": "wall-token"}
    for path in ("/api/summary", "/api/plan", "/api/coach"):
        assert kiosk.get(path, headers=T).status_code == 200
    assert kiosk.get("/api/summary", headers={"X-Kiosk-Token": "nope"}).status_code == 401
    # read-only, and only what the kiosk shows
    assert kiosk.get("/api/settings", headers=T).status_code == 401
    assert kiosk.get("/api/activities", headers=T).status_code == 401
    assert kiosk.post("/api/coach/refresh", headers={**T, **H}).status_code == 401


def test_kiosk_disabled_without_token_setting(client):
    c = TestClient(app)
    assert c.get("/kiosk?token=", follow_redirects=False).status_code == 303
    assert c.get("/api/summary", headers={"X-Kiosk-Token": ""}).status_code == 401


def test_kiosk_with_login(authed):
    assert authed.get("/kiosk").status_code == 200
