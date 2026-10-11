import dataclasses
import json
from datetime import date, timedelta

import httpx
import pytest

from app import coach, queries
from app.db import connect, kv_delete


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


@pytest.fixture
def ollama(imported, monkeypatch):
    monkeypatch.setattr(coach, "settings", dataclasses.replace(
        coach.settings, ollama_url="http://llm:11434", ollama_model="qwen-test"))
    for k in ("coach_note", "coach_status"):
        kv_delete(k)
    calls = []
    replies = []

    def post(url, json=None, timeout=None):
        calls.append({"url": url, "body": dict(json)})
        return replies.pop(0) if replies else Resp(200, {"message": {"content": "Easy day today."}})

    monkeypatch.setattr(httpx, "post", post)
    return calls, replies


def test_note_is_written_from_facts_and_cached(ollama):
    calls, _ = ollama
    note = coach.generate(what=("note",))
    assert note["text"] == "Easy day today." and note["model"] == "qwen-test"
    assert calls[0]["url"] == "http://llm:11434/api/chat"
    sent = json.loads(calls[0]["body"]["messages"][1]["content"])
    assert sent["date"] == queries.today().isoformat() and sent["recent_runs"]
    assert "Use ONLY the facts" in calls[0]["body"]["messages"][0]["content"]
    assert coach.generate(what=("note",)) == note and len(calls) == 1  # same facts: no new call
    assert coach.state()["note"]["text"] == "Easy day today."


def test_weekly_review(ollama, monkeypatch):
    calls, replies = ollama
    # the test runs were 3–4 days ago: look at them from the Monday after their week
    with connect() as c:
        last_run = date.fromisoformat(c.execute("SELECT MAX(local_date) FROM activities").fetchone()[0])
    monday = queries.week_start(last_run) + timedelta(days=7)
    monkeypatch.setattr(queries, "today", lambda: monday)
    replies.append(Resp(200, {"message": {"content": "Solid week.\n\nNext week builds."}}))
    coach.generate(what=("review",))
    sent = json.loads(calls[-1]["body"]["messages"][1]["content"])
    assert "short review" in calls[-1]["body"]["messages"][0]["content"]
    with connect() as c:
        expected = c.execute("SELECT COUNT(*) FROM activities WHERE is_run = 1 AND local_date BETWEEN ? AND ?",
                             ((monday - timedelta(days=7)).isoformat(), (monday - timedelta(days=1)).isoformat())).fetchone()[0]
    assert sent["reviewed_week"]["runs"] == expected >= 1
    assert sent["week"].startswith((monday - timedelta(days=7)).isoformat())
    st = coach.state()
    assert st["review"]["text"] == "Solid week.\n\nNext week builds."
    n = len(calls)
    coach.generate(what=("review",))
    assert len(calls) == n  # unchanged week: no new call
    monkeypatch.setattr(queries, "today", lambda: monday + timedelta(days=7))
    assert coach.state()["review"] is None  # a week later it's stale



@pytest.fixture
def sunday(imported, monkeypatch):
    """Sunday 12 May 2030, with a run on the Wednesday of that week."""
    sun = date(2030, 5, 12)
    monkeypatch.setattr(queries, "today", lambda: sun)

    def add_run(day: date, tag: str):
        with connect() as c:
            c.execute("INSERT INTO activities(file_hash, is_run, sport, start_utc, local_date, distance_m, trimp) "
                      "VALUES(?, 1, 'running', ?, ?, 10000, 80)", (tag, f"{day}T08:00:00", day.isoformat()))

    add_run(sun - timedelta(days=4), "test-wed")
    yield sun, lambda: add_run(sun, "test-sun")
    with connect() as c:
        c.execute("DELETE FROM activities WHERE file_hash IN ('test-wed', 'test-sun')")


def test_review_switches_to_this_week_on_sunday(sunday, monkeypatch):
    sun, run_today = sunday
    this_monday, last_monday = date(2030, 5, 6), date(2030, 4, 29)
    monkeypatch.setattr(coach, "_local_hour", lambda: 17)
    assert coach.review_week() == last_monday  # Sunday afternoon, no run yet: last week
    assert "week_ends_today" not in (coach.week_facts() or {})
    monkeypatch.setattr(coach, "_local_hour", lambda: 18)
    assert coach.review_week() == this_monday  # from 6 pm: the week just ending
    f = coach.week_facts()
    assert f["week"] == "2030-05-06 to 2030-05-12" and f["week_ends_today"] is True
    assert f["reviewed_week"]["runs"] == 1
    monkeypatch.setattr(coach, "_local_hour", lambda: 9)
    run_today()
    assert coach.review_week() == this_monday  # Sunday's run is in: no need to wait
    monkeypatch.setattr(queries, "today", lambda: sun + timedelta(days=1))
    assert coach.review_week() == this_monday  # Monday: same week, now "last week"


def test_review_card_says_this_week_on_sunday(ollama, sunday, monkeypatch):
    sun, _ = sunday
    monkeypatch.setattr(coach, "_local_hour", lambda: 19)
    kv_delete("weekly_review")
    coach.generate(what=("review",))
    assert "week_ends_today" in coach.REVIEW_PROMPT
    assert coach.state()["review"]["this_week"] is True
    monkeypatch.setattr(queries, "today", lambda: sun + timedelta(days=1))
    assert coach.state()["review"]["this_week"] is False  # same review, now last week's


def test_thinking_is_stripped_and_old_ollama_retried(ollama):
    calls, replies = ollama
    replies += [Resp(400, {"error": "think value not supported"}),
                Resp(200, {"message": {"content": "<think>hmm</think>\nGo run."}})]
    assert coach.generate(force=True)["text"] == "Go run."
    assert "think" in calls[0]["body"] and "think" not in calls[1]["body"]


def test_unreachable_model_is_reported(ollama, monkeypatch):
    def down(*a, **kw):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(httpx, "post", down)
    assert coach.generate(force=True) is None
    st = coach.state()["status"]
    assert st["ok"] is False and "connection refused" in st["error"]


def test_not_configured_does_nothing(imported):
    assert not coach.settings.coach_configured
    assert coach.generate(force=True) is None
    assert coach.generate_in_background() is False


@pytest.fixture
def long_run_yesterday(imported, monkeypatch):
    today = date(2030, 5, 6)
    monkeypatch.setattr(queries, "today", lambda: today)
    with connect() as c:
        c.execute("INSERT INTO activities(file_hash, is_run, sport, start_utc, local_date, distance_m, trimp) "
                  "VALUES('test-long', 1, 'running', '2030-05-05T08:00:00', ?, 21100, 210)",
                  ((today - timedelta(days=1)).isoformat(),))
    yield today
    with connect() as c:
        c.execute("DELETE FROM activities WHERE file_hash = 'test-long'")


def test_readiness_context_after_long_run(long_run_yesterday):
    t = long_run_yesterday
    rows = [{"date": (t - timedelta(days=i)).isoformat(), "resting_hr": 47, "sleep_s": 7 * 3600,
             "sleep_score": 80, "hrv_night": 100, "hrv_low": 91, "hrv_high": 113} for i in range(10, 0, -1)]
    rows.append({"date": t.isoformat(), "resting_hr": 54, "sleep_s": 6.8 * 3600, "sleep_score": 61,
                 "hrv_night": 69, "hrv_low": 91, "hrv_high": 113})
    r = queries.readiness(rows)
    assert r["level"] == "low" and "21.1 km long run" in r["context"]
    rows[-1].update(resting_hr=47, hrv_night=100)
    assert queries.readiness(rows)["context"] is None  # nothing to explain
