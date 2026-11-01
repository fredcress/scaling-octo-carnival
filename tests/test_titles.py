import dataclasses
import json
from datetime import date, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app import coach, main, strava, titles, watcher
from app.db import connect, kv_delete, kv_get, kv_set
from app.fitimport import reprocess

H = {"X-Requested-With": "fetch"}


class Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def weather_reply(day: str) -> Resp:
    hours = [f"{day}T{h:02d}:00" for h in range(24)]
    return Resp(200, {
        "hourly": {"time": hours, "temperature_2m": [8.4] * 24, "apparent_temperature": [5.2] * 24,
                   "precipitation": [0.6] * 24, "weather_code": [61] * 24, "wind_speed_10m": [21.0] * 24},
        "daily": {"sunrise": [f"{day}T06:50"], "sunset": [f"{day}T16:37"]}})


@pytest.fixture
def llm(imported, monkeypatch):
    """A configured model whose replies the test queues; weather from a fake Open-Meteo.
    Activity names are put back afterwards so other tests see the importer's names."""
    cfg = dataclasses.replace(coach.settings, ollama_url="http://llm:11434", ollama_model="qwen-test")
    for mod in (coach, titles, main):
        monkeypatch.setattr(mod, "settings", cfg)
    kv_delete("titles_status")
    kv_delete("title_prompt")
    with connect() as conn:
        saved = [dict(r) for r in conn.execute("SELECT id, name FROM activities")]
        conn.execute("UPDATE activities SET title_source = NULL, title_tries = 0, weather = NULL")
    sent, replies, weather_calls = [], [], []

    def post(url, json=None, timeout=None, **kw):
        sent.append(json)
        return replies.pop(0) if replies else Resp(200, {"message": {"content": "Rainy Riverside Shuffle"}})

    def get(url, params=None, timeout=None, **kw):
        weather_calls.append((url, params))
        return weather_reply(params["start_date"])

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    yield sent, replies, weather_calls
    with connect() as conn:
        conn.executemany("UPDATE activities SET name = ? WHERE id = ?", [(r["name"], r["id"]) for r in saved])
        conn.execute("UPDATE activities SET title_source = NULL, title_tries = 0, weather = NULL")


def run_id(file_name="run1.fit") -> int:
    with connect() as conn:
        return conn.execute("SELECT id FROM activities WHERE file_name = ?", (file_name,)).fetchone()["id"]


def row(aid) -> dict:
    with connect() as conn:
        return dict(conn.execute("SELECT * FROM activities WHERE id = ?", (aid,)).fetchone())


def test_occasions():
    assert titles.occasion(date(2026, 10, 31)) == "Halloween"
    assert titles.occasion(date(2026, 4, 5)) == "Easter Sunday"
    assert titles.occasion(date(2026, 4, 3)) == "Good Friday"
    assert titles.occasion(date(2027, 3, 28)) == "Easter Sunday"
    assert titles.occasion(date(2026, 12, 25)) == "Christmas Day"
    assert titles.occasion(date(2026, 10, 30)) is None


def test_clean():
    assert titles.clean('Title: "Ghosts Along the Riverbank."\n') == "Ghosts Along the Riverbank"
    assert titles.clean("**Pumpkin Lantern Loop**\nBecause it was Halloween") == "Pumpkin Lantern Loop"
    long = titles.clean("word " * 40)
    assert len(long) <= titles.MAX_LEN and not long.endswith(" ")
    for bad in ("Morning Run", "evening trail run", "", "  \n"):
        with pytest.raises(ValueError):
            titles.clean(bad)


def test_facts(llm):
    _, _, weather_calls = llm
    aid = run_id()
    f = titles.facts(aid)
    a = row(aid)
    assert f["activity"] == "Run" and f["distance_km"] == 5.4 and f["pace"].endswith("/km")
    assert f["weather"] == {"sky": "light rain", "temp_c": 8, "feels_like_c": 5, "wind_kmh": 21, "rain_mm": 0.6}
    lat, lon = weather_calls[0][1]["latitude"], weather_calls[0][1]["longitude"]
    assert lat == round(lat, 2) and lon == round(lon, 2)  # only a rounded position leaves the house
    assert json.loads(a["weather"])["sky"] == "light rain"
    assert datetime.fromisoformat(a["start_local"]).strftime("%A") in f["date"]
    titles.facts(aid)
    assert len(weather_calls) == 1  # fetched once, then kept


def test_no_weather_when_off(llm, monkeypatch):
    _, _, weather_calls = llm
    monkeypatch.setattr(titles, "settings", dataclasses.replace(titles.settings, weather=False))
    assert "weather" not in titles.facts(run_id())
    assert weather_calls == []


def test_light():
    w = {"sunrise": "2026-10-30T06:50", "sunset": "2026-10-30T16:37"}
    at = lambda h, m: datetime(2026, 10, 30, h, m)  # noqa: E731
    assert titles._light(w, at(19, 0), at(19, 40)) == "in the dark"
    assert titles._light(w, at(5, 0), at(5, 40)) == "in the dark"
    assert titles._light(w, at(16, 10), at(16, 50)) == "sunset during the activity"
    assert titles._light(w, at(6, 30), at(7, 10)) == "sunrise during the activity"
    assert titles._light(w, at(12, 0), at(12, 40)) is None


def test_write_title_and_keep_it(llm):
    sent, _, _ = llm
    aid = run_id()
    assert titles.write_title(aid) == "Rainy Riverside Shuffle"
    a = row(aid)
    assert a["name"] == "Rainy Riverside Shuffle" and a["title_source"] == "ai"
    assert sent[0]["messages"][0]["content"] == titles.TITLE_PROMPT
    assert sent[0]["options"]["temperature"] == 0.9
    reprocess(aid)  # recalculating stats keeps the title
    assert row(aid)["name"] == "Rainy Riverside Shuffle"
    # the next title sees this one, to avoid repeating it
    titles.write_title(run_id("run2.fit"))
    assert "Rainy Riverside Shuffle" in json.loads(sent[1]["messages"][1]["content"])["recent_titles"]


def test_notes_say_what_is_longest_and_are_not_titles(llm, monkeypatch):
    _, replies, _ = llm
    aid = run_id()
    monkeypatch.setattr(titles, "_compare", lambda conn, a: ["longest run ever"])
    assert titles.facts(aid)["compared_with_usual"] == ["longest run ever"]
    replies.append(Resp(200, {"message": {"content": "Longest run ever."}}))
    with pytest.raises(ValueError, match="one of the notes"):
        titles.write_title(aid)
    assert titles.write_title(aid) == "Rainy Riverside Shuffle"


def test_milestones():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript("CREATE TABLE activities(id, start_utc); CREATE TABLE best_efforts(activity_id, distance_m);"
                       "INSERT INTO activities VALUES (1, '2026-01-01'); INSERT INTO best_efforts VALUES (1, 21097.5);")
    run = lambda km, start="2026-06-01": {"is_run": 1, "distance_m": km * 1000, "start_utc": start}  # noqa: E731
    assert titles._milestones(conn, run(21.2)) == ["half marathon distance"]
    assert titles._milestones(conn, run(21.2, "2025-01-01")) == ["first half marathon in your records"]
    assert titles._milestones(conn, run(25)) == []  # well past it: the "longest" notes cover that
    assert titles._milestones(conn, run(42.3)) == ["first marathon in your records"]
    assert titles._milestones(conn, run(15)) == []
    assert titles._milestones(conn, {**run(21.2), "is_run": 0}) == []


def test_bad_answers_are_retried_then_left(llm):
    _, replies, _ = llm
    aid = run_id()
    for _ in range(titles.MAX_TRIES):
        replies.append(Resp(200, {"message": {"content": "Morning Run"}}))
        with pytest.raises(ValueError):
            titles.write_title(aid)
    assert row(aid)["title_tries"] == titles.MAX_TRIES and row(aid)["title_source"] is None
    assert aid not in titles.pending(all_dates=True)
    assert titles.state()["status"]["ok"] is False


def test_new_activities_get_titles(llm):
    pending = titles.pending()
    assert len(pending) == 3  # the demo activities are from the last few days
    assert titles.run_pending() == 3
    assert titles.pending() == []
    with connect() as conn:
        assert {r["name"] for r in conn.execute("SELECT name FROM activities")} == {"Rainy Riverside Shuffle"}
        assert {r["title_source"] for r in conn.execute("SELECT title_source FROM activities")} == {"ai"}


def test_title_days(llm, monkeypatch):
    with connect() as conn:
        conn.execute("UPDATE activities SET local_date = '2020-01-01' WHERE file_name = 'walk.fit'")
    try:
        assert len(titles.pending()) == 2  # the walk is too old for the default 7 days
        monkeypatch.setattr(titles, "settings", dataclasses.replace(titles.settings, title_days=99999))
        assert len(titles.pending()) == 3
    finally:
        reprocess(run_id("walk.fit"))  # puts its real date back


def test_unreachable_model_stops_the_loop(llm, monkeypatch):
    calls = []

    def down(*a, **kw):
        calls.append(1)
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", down)
    assert titles.run_pending() == 0
    assert len(calls) == 1  # gave up after the first, the next scan tries again


def test_nothing_without_a_model(imported):
    assert not titles.settings.coach_configured
    assert titles.run_pending() == 0
    assert titles.run_in_background() is False


def test_your_title_wins(llm):
    aid = run_id()
    titles.set_title(aid, "Parkrun with Sam")
    assert aid not in titles.pending(all_dates=True)
    reprocess(aid)
    assert row(aid)["name"] == "Parkrun with Sam"


def test_strava_api_upload_sends_the_title(llm, monkeypatch):
    from app import config
    monkeypatch.setattr(strava, "settings", config.Settings(
        **{**config.settings.__dict__, "strava_client_id": "1", "strava_client_secret": "s"}))
    kv_set("strava_tokens", {"access_token": "tok", "refresh_token": "r", "expires_at": 4e9})
    monkeypatch.setattr(strava.time, "sleep", lambda s: None)
    sent = []
    monkeypatch.setattr(httpx, "post", lambda url, **kw: sent.append(kw.get("data")) or Resp(
        201, {"id": 77, "status": "processing", "error": None, "activity_id": None}))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: Resp(200, {"id": 77, "error": None, "activity_id": 9}))
    aid = run_id()
    titles.set_title(aid, "Ghosts Along the Riverbank")
    with connect() as conn:
        conn.execute("UPDATE activities SET strava_status = 'none' WHERE id = ?", (aid,))
    strava.upload(aid)
    assert sent[0]["name"] == "Ghosts Along the Riverbank"
    with connect() as conn:
        conn.execute("UPDATE activities SET strava_status = 'none', strava_activity_id = NULL, "
                     "strava_upload_id = NULL WHERE id = ?", (aid,))


@pytest.fixture
def authed(llm, monkeypatch):
    monkeypatch.setattr(watcher, "start", lambda: None)
    with TestClient(main.app) as c:
        r = c.post("/login", data={"username": "runner", "password": "secret"}, follow_redirects=False)
        assert r.status_code == 303
        yield c


def test_api(authed):
    aid = run_id()
    assert authed.get("/api/me").json()["ai_titles"] is True
    r = authed.put(f"/api/activities/{aid}/title", json={"name": "  Pumpkin   Lantern Loop "}, headers=H)
    assert r.json() == {"name": "Pumpkin Lantern Loop", "title_source": "you"}
    assert authed.get(f"/api/activities/{aid}").json()["name"] == "Pumpkin Lantern Loop"
    assert authed.put(f"/api/activities/{aid}/title", json={"name": ""}, headers=H).status_code == 422
    assert authed.put("/api/activities/99999/title", json={"name": "x"}, headers=H).status_code == 404

    r = authed.post(f"/api/activities/{aid}/title", headers=H)
    assert r.json() == {"name": "Rainy Riverside Shuffle", "title_source": "ai"}
    a = authed.get(f"/api/activities/{aid}").json()
    assert a["title_source"] == "ai" and a["weather"]["sky"] == "light rain"

    s = authed.get("/api/settings").json()["titles"]
    assert s["configured"] and s["status"]["last"] == "Rainy Riverside Shuffle"
    assert s["untitled"] == 2


def test_custom_prompt(authed):
    r = authed.get("/api/titles/prompt").json()
    assert r == {"prompt": titles.TITLE_PROMPT, "default": titles.TITLE_PROMPT, "custom": False}
    assert authed.put("/api/titles/prompt", json={"prompt": "short"}, headers=H).status_code == 422
    mine = "Write a title like a pirate would. At most 60 characters."
    r = authed.put("/api/titles/prompt", json={"prompt": f"  {mine}\n"}, headers=H).json()
    assert r["prompt"] == mine and r["custom"] is True
    assert authed.get("/api/settings").json()["titles"]["custom_prompt"] is True
    assert titles.prompt() == mine
    r = authed.delete("/api/titles/prompt", headers=H).json()
    assert r["custom"] is False and r["prompt"] == titles.TITLE_PROMPT
    # saving the default text (or nothing) is the same as a reset
    titles.set_prompt(titles.TITLE_PROMPT)
    assert kv_get("title_prompt") is None


def test_custom_prompt_reaches_the_model(llm):
    sent, _, _ = llm
    titles.set_prompt("Only ever answer with a haiku-length title about cheese.")
    titles.write_title(run_id())
    assert sent[0]["messages"][0]["content"].startswith("Only ever answer")
