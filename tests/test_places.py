import dataclasses

import httpx
import pytest

from app import places
from app.db import kv_delete

LONDON = [[51.5072, -0.1276], [51.51, -0.12]]
RICHMOND = [[51.4613, -0.3037], [51.47, -0.30]]  # ~13 km from central London: same area
LYON = [[45.764, 4.8357], [45.77, 4.84]]


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


@pytest.fixture
def nominatim(imported, monkeypatch):
    monkeypatch.setattr(places, "settings", dataclasses.replace(places.settings, place_names=True))
    monkeypatch.setattr(places, "_last_failure", 0.0)
    monkeypatch.setattr(places.time, "sleep", lambda s: None)
    kv_delete("place_names")
    calls = []
    replies = {(51.5, -0.1): {"address": {"city": "Greater London", "country": "United Kingdom"}},
               (51.5, -0.2): {"address": {"city": "Greater London"}},
               (45.8, 4.8): {"address": {"village": "Saint-Didier", "municipality": "Lyon"}}}

    def get(url, params=None, headers=None, timeout=None):
        calls.append(params)
        assert headers["User-Agent"]
        return Resp(replies.get((params["lat"], params["lon"]), {}))

    monkeypatch.setattr(httpx, "get", get)
    yield calls
    kv_delete("place_names")


def test_distance():
    assert 340 < places.km_between([51.5072, -0.1276], [48.8566, 2.3522]) < 348  # London–Paris ≈ 344


def test_group_biggest_area_first():
    areas = places.group([LYON, LONDON, RICHMOND, LONDON])
    assert [a["count"] for a in areas] == [3, 1]
    london = areas[0]
    assert london["bounds"][0] == [51.4613, -0.3037] and london["bounds"][1] == [51.51, -0.12]
    assert places.group([]) == []


def test_names_are_looked_up_once(nominatim):
    calls = nominatim
    areas = places.named(places.group([LONDON, LONDON, LYON]))
    assert [a["name"] for a in areas] == ["London", "Lyon"]
    assert all(p["lat"] == round(p["lat"], 1) for p in calls)  # only a rounded point is sent
    n = len(calls)
    places.named(places.group([LONDON, LYON]))
    assert len(calls) == n  # cached


def test_fallback_names(nominatim, monkeypatch):
    def down(*a, **kw):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx, "get", down)
    assert [a["name"] for a in places.named(places.group([LONDON, LYON]))] == ["Area 1", "Area 2"]


def test_off(nominatim, monkeypatch):
    monkeypatch.setattr(places, "settings", dataclasses.replace(places.settings, place_names=False))
    assert places.named(places.group([LONDON]))[0]["name"] == "Area 1"
    assert nominatim == []
