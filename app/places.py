"""Heatmap areas: runs grouped by where they start, named with OpenStreetMap.

Runs that start within AREA_KM of an area's centre belong to it, so a home city and a
holiday town get their own view instead of one map zoomed out across a continent.
Names come from Nominatim (OpenStreetMap's place lookup), asked once per area with the
centre rounded to ~1 km and kept in the database; PLACE_NAMES=false turns this off.
"""
import logging
import math
import time

import httpx

from .config import settings
from .db import kv_get, kv_set

log = logging.getLogger(__name__)

AREA_KM = 50
NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "scaling-octo-carnival/1 (self-hosted running dashboard)"  # their usage policy asks for one
RETRY_AFTER_S = 3600  # after a failed lookup, don't ask again for an hour
_last_failure = 0.0


def km_between(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))


def group(lines: list) -> list[dict]:
    """Group polylines by start point. Biggest area first; each has its run count,
    centre and the bounds of all its points."""
    areas = []
    for line in lines:
        if not line:
            continue
        start = line[0]
        near = min(areas, key=lambda a: km_between(a["centre"], start), default=None)
        if near is None or km_between(near["centre"], start) > AREA_KM:
            near = {"centre": list(start), "count": 0, "south": 90, "west": 180, "north": -90, "east": -180}
            areas.append(near)
        n = near["count"]
        near["centre"] = [(near["centre"][0] * n + start[0]) / (n + 1),
                          (near["centre"][1] * n + start[1]) / (n + 1)]
        near["count"] = n + 1
        for lat, lon in line:
            near["south"], near["north"] = min(near["south"], lat), max(near["north"], lat)
            near["west"], near["east"] = min(near["west"], lon), max(near["east"], lon)
    areas.sort(key=lambda a: -a["count"])
    return [{"count": a["count"], "centre": [round(x, 3) for x in a["centre"]],
             "bounds": [[a["south"], a["west"]], [a["north"], a["east"]]]} for a in areas]


def _name_from(reply: dict) -> str | None:
    addr = reply.get("address") or {}
    for key in ("city", "municipality", "town", "village", "county", "state", "country"):
        if addr.get(key):
            name = addr[key]
            return name[len("Greater "):] if name.startswith("Greater ") else name
    return reply.get("name") or None


def name_for(lat: float, lon: float) -> str | None:
    """The place name for a point, from the cache or one Nominatim lookup."""
    global _last_failure
    key = f"{round(lat, 2)},{round(lon, 2)}"
    cache = kv_get("place_names") or {}
    if key in cache:
        return cache[key]
    if not settings.place_names or time.time() - _last_failure < RETRY_AFTER_S:
        return None
    try:
        r = httpx.get(NOMINATIM, timeout=6, headers={"User-Agent": USER_AGENT}, params={
            "lat": round(lat, 2), "lon": round(lon, 2), "zoom": 10,
            "format": "jsonv2", "accept-language": "en"})
        r.raise_for_status()
        name = _name_from(r.json())
    except (httpx.HTTPError, ValueError) as e:
        log.warning("place name for %s: %s", key, e)
        _last_failure = time.time()
        return None
    cache[key] = name
    kv_set("place_names", cache)
    return name


def named(areas: list[dict]) -> list[dict]:
    """Give each area a name: the place, or "Area 2" when there's none."""
    asked = False
    for i, a in enumerate(areas):
        lat, lon = a["centre"]
        new = f"{round(lat, 2)},{round(lon, 2)}" not in (kv_get("place_names") or {})
        if new and asked:
            time.sleep(1)  # Nominatim allows one request a second
        a["name"] = name_for(lat, lon) or f"Area {i + 1}"
        asked = asked or new
    return areas
