import httpx

from app import strava
from app.db import connect, kv_set


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _setup(monkeypatch, upload_status):
    from app import config
    monkeypatch.setattr(strava, "settings", config.Settings(
        **{**config.settings.__dict__, "strava_client_id": "1", "strava_client_secret": "s"}))
    kv_set("strava_tokens", {"access_token": "tok", "refresh_token": "r", "expires_at": 4e9})
    monkeypatch.setattr(strava.time, "sleep", lambda s: None)
    calls = []

    def post(url, **kw):
        calls.append(url)
        return FakeResponse(201, {"id": 77, "status": "Your activity is still being processed.",
                                  "error": None, "activity_id": None})

    def get(url, **kw):
        return FakeResponse(200, upload_status)

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return calls


def _first_run():
    with connect() as conn:
        return conn.execute("SELECT id FROM activities WHERE is_run = 1 ORDER BY id").fetchone()["id"]


def _state(aid):
    with connect() as conn:
        return dict(conn.execute("SELECT strava_status, strava_activity_id, strava_error "
                                 "FROM activities WHERE id = ?", (aid,)).fetchone())


def test_upload_done(imported, monkeypatch):
    calls = _setup(monkeypatch, {"id": 77, "error": None, "activity_id": 123456})
    aid = _first_run()
    strava.upload(aid)
    assert calls[0].endswith("/uploads")
    assert _state(aid) == {"strava_status": "done", "strava_activity_id": 123456, "strava_error": None}


def test_upload_duplicate(imported, monkeypatch):
    err = "run2.fit duplicate of <a href='https://www.strava.com/activities/987654' target='_blank'>Morning Run</a>"
    _setup(monkeypatch, {"id": 77, "error": err, "activity_id": None})
    aid = _first_run() + 1
    strava.upload(aid)
    assert _state(aid)["strava_status"] == "duplicate"
    assert _state(aid)["strava_activity_id"] == 987654


def test_upload_error(imported, monkeypatch):
    _setup(monkeypatch, {"id": 77, "error": "The file is empty", "activity_id": None})
    aid = _first_run() + 2
    strava.upload(aid)
    assert _state(aid) == {"strava_status": "error", "strava_activity_id": None,
                           "strava_error": "The file is empty"}
