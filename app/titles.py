"""Activity titles written by the local LLM, instead of "Afternoon Run".

The app collects what is worth knowing about the activity (the day, any occasion, the
weather, darkness, how it compares with usual, PRs, the planned session) and the model
turns it into one short title. Titles replace `activities.name`, so every page, the coach
and the Strava upload use them; `title_source` says who wrote it ('ai' or 'you').
"""
import json
import logging
import re
import statistics
import threading
from datetime import date, datetime, timedelta, timezone

import httpx

from . import analytics as an
from . import coach, queries
from .config import settings
from .db import connect, kv_get, kv_set
from .fitimport import sport_label

log = logging.getLogger(__name__)

MAX_TRIES = 3  # automatic attempts per activity before giving up
MAX_LEN = 80
_model = threading.Lock()  # one title request at a time
_worker = threading.Lock()  # one background loop at a time

TITLE_PROMPT = """You give a runner's activities their titles on Strava. Write ONE title \
for the activity described in the JSON.

Rules:
- Use only the facts in the JSON. Never invent places, people, weather or events.
- "milestones", "compared_with_usual" and "personal_records" are notes about this \
activity, not titles: build on them in your own words, never copy one as the title.
- Only mention an occasion if "occasion" names it. "Longest" in the notes is about \
distance, never about the day.
- Let the activity set the tone. An ordinary run gets a short title, plain or a little wry. \
Go poetic or playful only when something stands out: an occasion, striking weather, \
darkness, a sunrise or sunset, a personal record, an unusually long or hard effort, a race.
- A big day (anything in "milestones", a personal record, the longest run ever, race day) \
deserves a proud, memorable title, not a status report.
- At most 60 characters. No quotation marks, hashtags or emoji, no full stop at the end.
- Never a generic name like "Morning Run" or "Evening Ride", and don't repeat the wording \
of recent_titles.
- British English. Reply with the title only.

Examples of the range, for tone only (never reuse them): an ordinary easy run in drizzle \
"Tuesday legs, Tuesday drizzle"; a dark winter evening "Headtorch weather"; a new 5 km \
record "Five kilometres, new best, no regrets"; a first half marathon on a sunny morning \
"Twenty-one point one and a golden sky"."""

GENERIC = re.compile(r"^(early )?(morning|lunch|afternoon|evening|night) [a-z -]+$", re.I)

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog",
       48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
       56: "freezing drizzle", 57: "freezing drizzle", 61: "light rain", 63: "rain",
       65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
       75: "heavy snow", 77: "snow grains", 80: "light showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "heavy snow showers", 95: "thunderstorm",
       96: "thunderstorm with hail", 99: "thunderstorm with hail"}

FIXED_OCCASIONS = {(1, 1): "New Year's Day", (2, 14): "Valentine's Day",
                   (3, 17): "St Patrick's Day", (4, 1): "April Fools' Day",
                   (6, 21): "the longest day of the year", (10, 31): "Halloween",
                   (12, 21): "the shortest day of the year", (12, 24): "Christmas Eve",
                   (12, 25): "Christmas Day", (12, 26): "Boxing Day", (12, 31): "New Year's Eve"}


def _easter(year: int) -> date:
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    return date(year, month, day + 1)


def occasion(d: date) -> str | None:
    easter = _easter(d.year)
    moving = {easter - timedelta(days=2): "Good Friday", easter: "Easter Sunday",
              easter + timedelta(days=1): "Easter Monday"}
    if d.month == 11 and d.day == 5 and settings.tz == "Europe/London":
        return "Bonfire Night"
    return moving.get(d) or FIXED_OCCASIONS.get((d.month, d.day))


def _season(d: date) -> str:
    return ["winter", "spring", "summer", "autumn"][(d.month % 12) // 3]


def _part_of_day(h: int) -> str:
    return ("night" if h < 5 else "early morning" if h < 8 else "morning" if h < 12
            else "lunchtime" if h < 14 else "afternoon" if h < 18 else "evening" if h < 22
            else "night")


def _hm(seconds) -> str:
    seconds = int(seconds or 0)
    h, m = divmod(seconds // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m} min"


def _pace(sec_per_km) -> str | None:
    if not sec_per_km:
        return None
    s = round(sec_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


# ------------------------------------------------------------------ weather

def fetch_weather(lat: float, lon: float, start_local: datetime, end_local: datetime) -> dict:
    """Weather at the middle of the activity from Open-Meteo, plus sunrise and sunset.
    Only a ~1 km rounded position is sent."""
    day = start_local.date()
    old = (date.today() - day).days > 80  # the forecast API keeps about three months
    url = ("https://archive-api.open-meteo.com/v1/archive" if old
           else "https://api.open-meteo.com/v1/forecast")
    r = httpx.get(url, timeout=20, params={
        "latitude": round(lat, 2), "longitude": round(lon, 2),
        "start_date": day.isoformat(), "end_date": day.isoformat(), "timezone": "auto",
        "hourly": "temperature_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
        "daily": "sunrise,sunset"})
    r.raise_for_status()
    d = r.json()
    hourly, daily = d["hourly"], d["daily"]
    mid = start_local + (end_local - start_local) / 2
    i = min(range(len(hourly["time"])),
            key=lambda j: abs(datetime.fromisoformat(hourly["time"][j]) - mid))
    val = lambda k: hourly.get(k, [None] * (i + 1))[i]  # noqa: E731
    out = {"sky": WMO.get(val("weather_code")), "temp_c": val("temperature_2m"),
           "feels_like_c": val("apparent_temperature"), "rain_mm": val("precipitation"),
           "wind_kmh": val("wind_speed_10m"),
           "sunrise": (daily.get("sunrise") or [None])[0], "sunset": (daily.get("sunset") or [None])[0]}
    return {k: round(v) if isinstance(v, float) and k != "rain_mm" else v for k, v in out.items()
            if v is not None}


def _light(w: dict, start: datetime, end: datetime) -> str | None:
    """Darkness, sunrise or sunset during the activity, from the weather's sun times."""
    try:
        rise = datetime.fromisoformat(w["sunrise"])
        sset = datetime.fromisoformat(w["sunset"])
    except (KeyError, TypeError, ValueError):
        return None
    margin = timedelta(minutes=20)
    if start - margin <= rise <= end + margin:
        return "sunrise during the activity"
    if start - margin <= sset <= end + margin:
        return "sunset during the activity"
    if end < rise or start > sset + timedelta(minutes=30):
        return "in the dark"
    return None


def weather_for(a: dict) -> dict | None:
    """Stored weather, fetching it the first time. None if there's none to have."""
    if a["weather"] is not None:
        return json.loads(a["weather"]) or None
    if not settings.weather or not a["polyline"]:
        return None
    lat, lon = json.loads(a["polyline"])[0]
    start = datetime.fromisoformat(a["start_local"])
    end = start + timedelta(seconds=a["elapsed_s"] or a["moving_s"] or 0)
    try:
        w = fetch_weather(lat, lon, start, end)
    except (httpx.HTTPError, KeyError, ValueError, IndexError) as e:
        log.warning("weather for activity %s: %s", a["id"], e)
        return None  # try again next time
    with connect() as conn:
        conn.execute("UPDATE activities SET weather = ? WHERE id = ?", (json.dumps(w), a["id"]))
    return w or None


# ------------------------------------------------------------------ facts

def _compare(conn, a: dict) -> list[str]:
    """What stands out against the runner's usual: distance, pace, a long gap."""
    notes = []
    kind = sport_label(a["sport"], a["sub_sport"] or "generic").lower()  # "run", "trail run"…
    prior = conn.execute(
        "SELECT start_utc, distance_m, moving_s FROM activities WHERE sport = ? AND start_utc < ? "
        "AND id != ? ORDER BY start_utc DESC", (a["sport"], a["start_utc"], a["id"])).fetchall()
    if len(prior) < 5 or not a["distance_m"]:
        return notes
    start = datetime.fromisoformat(a["start_utc"])
    longer = next((p for p in prior if (p["distance_m"] or 0) >= a["distance_m"]), None)
    if longer is None:
        notes.append(f"longest {kind} ever")
    else:
        weeks = (start - datetime.fromisoformat(longer["start_utc"])).days // 7
        if weeks >= 4:
            notes.append(f"longest {kind} in {weeks} weeks")
    gap = (start - datetime.fromisoformat(prior[0]["start_utc"])).days
    if gap >= 5:
        notes.append(f"first {kind} in {gap} days")
    if a["is_run"] and a["moving_s"]:
        cutoff = (start - timedelta(days=90)).isoformat()
        paces = [p["moving_s"] / p["distance_m"] * 1000 for p in prior
                 if p["start_utc"] >= cutoff and p["distance_m"] and p["moving_s"]]
        if len(paces) >= 5:
            diff = a["moving_s"] / a["distance_m"] * 1000 - statistics.median(paces)
            if abs(diff) >= 10:
                notes.append(f"pace {abs(round(diff))} s/km {'slower' if diff > 0 else 'faster'} than usual")
    return notes


def _prs(conn, a: dict) -> list[str]:
    """Best efforts in this activity that beat every earlier one at that distance."""
    out = []
    for e in conn.execute("SELECT distance_m, time_s FROM best_efforts WHERE activity_id = ? "
                          "ORDER BY distance_m", (a["id"],)):
        best_before = conn.execute(
            "SELECT MIN(b.time_s) FROM best_efforts b JOIN activities x ON x.id = b.activity_id "
            "WHERE b.distance_m = ? AND x.start_utc < ?", (e["distance_m"], a["start_utc"])).fetchone()[0]
        if best_before is not None and e["time_s"] < best_before:
            out.append(an.BEST_EFFORT_DISTANCES.get(e["distance_m"], f"{e['distance_m'] / 1000:g} km"))
    return out


# race distances a run can "cover": (metres, name)
RACE_DISTANCES = [(21097.5, "half marathon"), (42195, "marathon")]


def _milestones(conn, a: dict) -> list[str]:
    """A run that covers a half or full marathon, flagged as the first in the records if so."""
    out = []
    dist = a["distance_m"] or 0
    for metres, name in RACE_DISTANCES:
        if not a["is_run"] or dist < metres * 0.995:
            continue
        earlier = conn.execute(
            "SELECT 1 FROM best_efforts b JOIN activities x ON x.id = b.activity_id "
            "WHERE b.distance_m = ? AND x.start_utc < ? LIMIT 1", (metres, a["start_utc"])).fetchone()
        if not earlier:
            out.append(f"first {name} in your records")
        elif dist <= metres * 1.06:
            out.append(f"{name} distance")
    return out[-1:]  # a marathon also covers a half: only say the bigger one


def _planned(a: dict) -> dict | None:
    cfg = kv_get("plan")
    if not cfg:
        return None
    try:
        p = queries.plan_view(cfg)
    except Exception:  # an old or odd plan must never stop a title
        log.exception("plan for title")
        return None
    if cfg.get("race_date") == a["local_date"]:
        return {"race_day": cfg.get("race_name") or p.get("label")}
    for w in p.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("date") == a["local_date"] and s.get("type") != "rest":
                return {"session": s.get("title"), "detail": s.get("detail")}
    return None


def _effort(avg_hr) -> str | None:
    if not avg_hr:
        return None
    zone = sum(1 for lim in an.hr_zone_limits(settings.max_hr, settings.resting_hr) if avg_hr >= lim)
    return {0: "very easy", 1: "easy", 2: "easy", 3: "steady", 4: "hard", 5: "very hard"}.get(zone)


def facts(activity_id: int) -> dict:
    with connect() as conn:
        a = dict(conn.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone())
        compare = _compare(conn, a)
        prs = _prs(conn, a) if a["is_run"] else []
        milestones = _milestones(conn, a)
        recent = [r["name"] for r in conn.execute(
            "SELECT name FROM activities WHERE title_source IS NOT NULL AND id != ? "
            "ORDER BY start_utc DESC LIMIT 8", (activity_id,))]
    start = datetime.fromisoformat(a["start_local"])
    end = start + timedelta(seconds=a["elapsed_s"] or a["moving_s"] or 0)
    km = (a["distance_m"] or 0) / 1000
    out = {
        "activity": sport_label(a["sport"], a["sub_sport"] or "generic"),
        "date": start.strftime("%A %-d %B %Y"), "season": _season(start.date()),
        "start_time": start.strftime("%H:%M"), "part_of_day": _part_of_day(start.hour),
        "occasion": occasion(start.date()),
        "distance_km": round(km, 1) if km else None, "duration": _hm(a["moving_s"]),
        "pace": _pace(a["moving_s"] / km) if a["is_run"] and km else None,
        "elevation_gain_m": round(a["ascent_m"]) if a["ascent_m"] and a["ascent_m"] >= 50 else None,
        "effort": _effort(a["avg_hr"]),
        "milestones": milestones, "compared_with_usual": compare, "personal_records": prs,
        "planned": _planned(a),
    }
    w = weather_for(a)
    if w:
        out["weather"] = {k: w[k] for k in ("sky", "temp_c", "feels_like_c", "wind_kmh", "rain_mm") if k in w}
        out["light"] = _light(w, start, end)
    out["recent_titles"] = recent
    return {k: v for k, v in out.items() if v not in (None, [], {})}


# ------------------------------------------------------------------ writing titles

def clean(text: str) -> str:
    """One tidy line from the model's answer; ValueError if it isn't a usable title."""
    line = next((x for x in text.splitlines() if x.strip()), "")
    line = re.sub(r"^\s*(title\s*:\s*)", "", line, flags=re.I)
    line = line.strip().strip("*_`\"'“”‘’ ").rstrip(".").strip()
    line = re.sub(r"\s+", " ", line)
    if len(line) > MAX_LEN:
        line = line[:MAX_LEN].rsplit(" ", 1)[0]
    if len(line) < 3 or GENERIC.match(line):
        raise ValueError(f"not a usable title: {text[:80]!r}")
    return line


def _status(**kw) -> None:
    kv_set("titles_status", {**(kv_get("titles_status") or {}), **kw,
                             "at": datetime.now(timezone.utc).isoformat()})


def write_title(activity_id: int, wait: float = 0) -> str:
    """Ask the model for a title and save it. Raises on failure (and counts the try)."""
    got = _model.acquire(timeout=wait) if wait else _model.acquire(blocking=False)
    if not got:
        raise RuntimeError("the model is busy with another title, try again in a minute")
    try:
        try:
            f = facts(activity_id)
            title = clean(coach.ask_model(f, TITLE_PROMPT, temperature=0.9))
            notes = {n.lower() for k in ("milestones", "compared_with_usual", "personal_records")
                     for n in f.get(k, [])}
            if title.lower() in notes:  # a note copied as the title, e.g. "Longest run ever"
                raise ValueError(f"not a usable title: {title!r} is one of the notes")
        except Exception as e:
            with connect() as conn:
                conn.execute("UPDATE activities SET title_tries = title_tries + 1 WHERE id = ?",
                             (activity_id,))
            _status(ok=False, error=f"{type(e).__name__}: {e}")
            raise
        with connect() as conn:
            conn.execute("UPDATE activities SET name = ?, title_source = 'ai', title_tries = title_tries + 1 "
                         "WHERE id = ?", (title, activity_id))
        _status(ok=True, error=None, last=title)
        log.info("title for activity %s: %s", activity_id, title)
        return title
    finally:
        _model.release()


def set_title(activity_id: int, title: str) -> None:
    """A title typed by the runner; the AI never replaces it on its own."""
    with connect() as conn:
        conn.execute("UPDATE activities SET name = ?, title_source = 'you' WHERE id = ?",
                     (title, activity_id))


def pending(all_dates: bool = False) -> list[int]:
    # activities from the last TITLE_DAYS days get a title on their own
    since = (queries.today() - timedelta(days=settings.title_days)).isoformat()
    with connect() as conn:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM activities WHERE title_source IS NULL AND title_tries < ? "
            f"{'' if all_dates else 'AND local_date >= ?'} ORDER BY start_utc DESC",
            (MAX_TRIES,) if all_dates else (MAX_TRIES, since))]


def run_pending(all_dates: bool = False) -> int:
    """Title every activity still waiting for one. Returns how many got a title."""
    if not settings.coach_configured or not _worker.acquire(blocking=False):
        return 0
    try:
        ids = pending(all_dates)
        done = failed = 0
        if all_dates:
            _status(backfill={"done": 0, "failed": 0, "total": len(ids)})
        for i, activity_id in enumerate(ids):
            try:
                write_title(activity_id, wait=coach.TIMEOUT_S)
                done += 1
            except Exception as e:
                failed += 1
                log.warning("title for activity %s: %s", activity_id, e)
                if isinstance(e, httpx.TransportError):
                    break  # the model isn't reachable: stop, the next scan tries again
            if all_dates:
                _status(backfill={"done": i + 1, "failed": failed, "total": len(ids)})
        if all_dates:
            _status(backfill={"done": len(ids), "failed": failed, "total": len(ids), "finished": True})
        return done
    finally:
        _worker.release()


def run_in_background(all_dates: bool = False) -> bool:
    """Start titling unless a loop is already running. True if started."""
    if not settings.coach_configured or _worker.locked():
        return False
    if not all_dates and not pending():
        return False
    threading.Thread(target=run_pending, kwargs={"all_dates": all_dates},
                     name="titles", daemon=True).start()
    return True


def state() -> dict:
    with connect() as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM activities WHERE title_source IS NULL").fetchone()[0]
    return {"configured": settings.coach_configured, "weather": settings.weather,
            "title_days": settings.title_days,
            "running": _worker.locked(), "untitled": waiting, "status": kv_get("titles_status")}
