"""Daily coach note written by a local LLM (Ollama).

The app computes every number; the model only turns those facts into a few
sentences. Notes are generated in the background (a 27B model can take a minute)
and cached until the facts change, so pages never wait on the model.
"""
import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import queries
from .config import settings
from .db import connect, kv_get, kv_set

log = logging.getLogger(__name__)

MIN_INTERVAL_S = 600  # don't regenerate more often than this unless asked
TIMEOUT_S = 300
_lock = threading.Lock()

SYSTEM_PROMPT = """You are a calm, experienced running coach writing a short morning note \
for one runner, based on their watch data and training plan.

Rules:
- Use ONLY the facts in the JSON. Never invent numbers, sessions or events.
- 3 to 5 sentences of plain English, second person. No greeting, headings, lists or emoji.
- Follow the readiness level: "good" means train as planned, "ok" means do the session but \
keep easy parts easy, "low" means suggest swapping a hard session for an easy run or rest.
- If readiness has a context (for example yesterday's long run), say the dip is expected.
- Mention today's planned session if there is one, and what to do with it. The plan already \
re-arranges itself around what was run (plan_changes_this_week) and gives advice: agree with \
it rather than proposing different changes. The runner never has to confirm anything.
- Do not give medical advice or diagnoses. If something looks off for several days with no \
training reason, suggest an easier day rather than speculating why."""


def _hm(seconds) -> str | None:
    if seconds is None:
        return None
    return f"{int(seconds // 3600)}h{int(seconds % 3600 // 60):02d}"


def _pace(sec_per_km) -> str | None:
    if not sec_per_km:
        return None
    s = round(sec_per_km)
    return f"{s // 60}:{s % 60:02d}/km"


def facts() -> dict:
    """Everything the note may talk about, already computed and rounded."""
    t = queries.today()
    out = {"date": t.isoformat(), "weekday": t.strftime("%A")}

    r = queries.readiness()
    if r:
        out["readiness"] = {k: r[k] for k in ("level", "flags", "context")}

    with connect() as conn:
        rows = [dict(x) for x in conn.execute(
            "SELECT * FROM health_daily WHERE date >= ? ORDER BY date",
            ((t - timedelta(days=30)).isoformat(),))]
        runs = conn.execute(
            "SELECT local_date, name, distance_m, moving_s, avg_hr, trimp FROM activities "
            "WHERE is_run = 1 AND local_date >= ? ORDER BY start_utc",
            ((t - timedelta(days=3)).isoformat(),)).fetchall()
    last = rows[-1] if rows and rows[-1]["date"] == t.isoformat() else None
    if last:
        rhr_past = sorted(x["resting_hr"] for x in rows[:-1] if x["resting_hr"])
        out["last_night"] = {
            "asleep": _hm(last["sleep_s"]), "deep": _hm(last["deep_s"]), "rem": _hm(last["rem_s"]),
            "sleep_score": last["sleep_score"],
            "hrv_ms": last["hrv_night"],
            "hrv_usual_range": [last["hrv_low"], last["hrv_high"]] if last["hrv_low"] else None,
            "resting_hr": last["resting_hr"],
            "resting_hr_usual": rhr_past[len(rhr_past) // 2] if rhr_past else None,
        }
    week = [x["sleep_s"] for x in rows[-7:] if x["sleep_s"]]
    if week:
        out["sleep_7day_avg"] = _hm(sum(week) / len(week))

    out["recent_runs"] = [{
        "date": x["local_date"], "name": x["name"], "km": round((x["distance_m"] or 0) / 1000, 1),
        "time": _hm(x["moving_s"]),
        "pace": _pace(x["moving_s"] / x["distance_m"] * 1000 if x["distance_m"] else None),
        "avg_hr": round(x["avg_hr"]) if x["avg_hr"] else None, "load": round(x["trimp"] or 0),
    } for x in runs]

    cfg = kv_get("plan")
    if cfg:
        p = queries.plan_view(cfg)
        if not p["finished"]:
            cur = next((w for w in p["weeks"] if w["index"] == p["current_week"]), None)
            sessions = cur["sessions"] if cur else []
            nxt = next((w for w in p["weeks"] if p["current_week"] and w["index"] == p["current_week"] + 1), None)
            upcoming = sessions + (nxt["sessions"] if nxt else [])
            tomorrow = (t + timedelta(days=1)).isoformat()
            pick = lambda d: next(({k: s[k] for k in ("title", "detail", "distance_km", "status")}  # noqa: E731
                                   for s in upcoming if s["date"] == d), "rest day")
            out["plan"] = {
                "race": cfg.get("race_name") or p["label"], "race_date": cfg["race_date"],
                "days_to_race": p["days_to_race"],
                "phase": cur["phase"] if cur else None,
                "today": pick(t.isoformat()), "tomorrow": pick(tomorrow),
                "week_planned_km": cur["planned_km"] if cur else None,
                "week_done_km": cur["actual_km"] if cur else None,
                # what the app already re-planned from the runs recorded, and its readiness tip
                "plan_changes_this_week": cur["changes"] if cur else [],
                "advice": p["advice"],
            }
    return out


REVIEW_PROMPT = """You are a calm, experienced running coach writing a short review of the \
runner's last training week, from their watch data and training plan.

Rules:
- Use ONLY the facts in the JSON. Never invent numbers, sessions or events.
- Two or three short paragraphs of plain English, second person, under 160 words in total. \
No greeting, headings, lists or emoji.
- Cover: how the week went against the plan (if there is one), what sleep, HRV and resting \
heart rate suggest about recovery, one thing that went well, one thing to watch, and what \
the coming week holds.
- "reviewed_week" is the week you are reviewing; compare it with "week_before" only where \
both have the figure.
- Do not give medical advice or diagnoses."""


def _week_summary(runs: list, health_rows: list) -> dict:
    km = sum((r["distance_m"] or 0) for r in runs) / 1000
    secs = sum((r["moving_s"] or 0) for r in runs)
    out = {"runs": len(runs), "km": round(km, 1), "time": _hm(secs),
           "avg_pace": _pace(secs / km if km else None),
           "longest_km": round(max(((r["distance_m"] or 0) for r in runs), default=0) / 1000, 1),
           "load": round(sum((r["trimp"] or 0) for r in runs))}
    sleep = [h["sleep_s"] for h in health_rows if h["sleep_s"]]
    hrv = [h["hrv_night"] for h in health_rows if h["hrv_night"]]
    rhr = [h["resting_hr"] for h in health_rows if h["resting_hr"]]
    if sleep:
        out["avg_sleep"] = _hm(sum(sleep) / len(sleep))
    if hrv:
        out["avg_hrv_ms"] = round(sum(hrv) / len(hrv))
        below = [h for h in health_rows if h["hrv_night"] and h["hrv_low"] and h["hrv_night"] < h["hrv_low"]]
        out["nights_hrv_below_usual"] = len(below)
    if rhr:
        out["avg_resting_hr"] = round(sum(rhr) / len(rhr), 1)
    return out


def week_facts() -> dict | None:
    """The last full Monday–Sunday week, with the one before for comparison."""
    t = queries.today()
    start = queries.week_start(t) - timedelta(days=7)
    prev = start - timedelta(days=7)
    end = start + timedelta(days=6)
    with connect() as conn:
        runs = [dict(r) for r in conn.execute(
            "SELECT local_date, name, distance_m, moving_s, trimp FROM activities WHERE is_run = 1 "
            "AND local_date BETWEEN ? AND ? ORDER BY start_utc", (prev.isoformat(), end.isoformat()))]
        health_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM health_daily WHERE date BETWEEN ? AND ?", (prev.isoformat(), end.isoformat()))]
    in_week = lambda rows, a: [r for r in rows if a.isoformat() <= (r.get("local_date") or r.get("date")) <= (a + timedelta(days=6)).isoformat()]  # noqa: E731
    this, before = in_week(runs, start), in_week(runs, prev)
    if not this and not before:
        return None
    out = {"week": f"{start.isoformat()} to {end.isoformat()}",
           "reviewed_week": _week_summary(this, in_week(health_rows, start)),
           "week_before": _week_summary(before, in_week(health_rows, prev))}
    out["reviewed_week"]["runs_list"] = [{"day": datetime.fromisoformat(r["local_date"]).strftime("%A"),
                                      "name": r["name"], "km": round((r["distance_m"] or 0) / 1000, 1)}
                                     for r in this]
    hr = [h for h in in_week(health_rows, start) if h["hrv_low"]]
    if hr:
        out["hrv_usual_range"] = [hr[-1]["hrv_low"], hr[-1]["hrv_high"]]

    cfg = kv_get("plan")
    if cfg:
        p = queries.plan_view(cfg)
        wk = next((w for w in p["weeks"] if w["start"] == start.isoformat()), None)
        nxt = next((w for w in p["weeks"] if w["start"] == queries.week_start(t).isoformat()), None)
        if wk or nxt:
            out["plan"] = {"race": cfg.get("race_name") or p["label"], "race_date": cfg["race_date"],
                           "days_to_race": p["days_to_race"]}
        if wk:
            out["plan"]["reviewed_week"] = {
                "phase": wk["phase"], "easier_week": wk["recovery"],
                "planned_km": wk["planned_km"], "done_km": wk["actual_km"],
                "sessions": [{"day": datetime.fromisoformat(s["date"]).strftime("%A"), "title": s["title"],
                              "status": s["status"], **({"moved": True} if s.get("moved_from") else {}),
                              **({"changed": s["changed"]} if s.get("changed") else {})}
                             for s in wk["sessions"]],
            }
        if nxt:
            out["plan"]["coming_week"] = {
                "phase": nxt["phase"], "easier_week": nxt["recovery"], "planned_km": nxt["planned_km"],
                "key_sessions": [f"{datetime.fromisoformat(s['date']).strftime('%A')}: {s['title']} — {s['detail']}"
                                 for s in nxt["sessions"] if s["type"] != "easy"],
            }
    return out


def _digest(f: dict) -> str:
    return hashlib.sha256(json.dumps(f, sort_keys=True, default=str).encode()).hexdigest()[:16]


def ask_model(f: dict, system: str = SYSTEM_PROMPT) -> str:
    body = {
        "model": settings.ollama_model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": json.dumps(f, ensure_ascii=False, indent=1)}],
        "stream": False,
        "think": False,  # reasoning models: we only want the note
        "options": {"temperature": 0.4},
    }
    url = f"{settings.ollama_url}/api/chat"
    r = httpx.post(url, json=body, timeout=TIMEOUT_S)
    if r.status_code == 400 and "think" in r.text:  # older Ollama, or a model without thinking
        body.pop("think")
        r = httpx.post(url, json=body, timeout=TIMEOUT_S)
    r.raise_for_status()
    text = r.json().get("message", {}).get("content", "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise ValueError("the model returned an empty answer")
    return text


def _write(key: str, f: dict, system: str, force: bool, extra: dict) -> dict | None:
    """Ask the model for a new text under `key` unless the facts are unchanged."""
    digest = _digest(f)
    old = kv_get(key) or {}
    same_period = all(old.get(k) == v for k, v in extra.items())
    recent = same_period and time.time() - old.get("ts", 0) < MIN_INTERVAL_S
    if not force and same_period and (old.get("digest") == digest or recent):
        return old
    started = time.time()
    try:
        text = ask_model(f, system)
    except (httpx.HTTPError, ValueError, KeyError) as e:
        kv_set("coach_status", {"ok": False, "error": f"{type(e).__name__}: {e}",
                                "at": datetime.now(timezone.utc).isoformat()})
        log.warning("coach: %s", e)
        return None
    new = {"text": text, **extra, "digest": digest, "model": settings.ollama_model,
           "ts": time.time(), "at": datetime.now(timezone.utc).isoformat(),
           "seconds": round(time.time() - started)}
    kv_set(key, new)
    kv_set("coach_status", {"ok": True, "error": None, "at": new["at"]})
    log.info("coach: new %s in %ss", key, new["seconds"])
    return new


def generate(force: bool = False, what: tuple = ("note", "review")) -> dict | None:
    """Write the daily note and the weekly review if their facts changed.
    Safe to call often; one model call at a time."""
    if not settings.coach_configured or not _lock.acquire(blocking=False):
        return None
    try:
        note = None
        if "note" in what:
            f = facts()
            note = _write("coach_note", f, SYSTEM_PROMPT, force, {"date": f["date"]})
        if "review" in what:
            wf = week_facts()
            if wf:
                _write("weekly_review", wf, REVIEW_PROMPT, force and "note" not in what,
                       {"week": wf["week"]})
        return note
    finally:
        _lock.release()


def generate_in_background(force: bool = False, what: tuple = ("note", "review")) -> bool:
    """Start generating unless the model is already busy. Returns True if started."""
    if not settings.coach_configured or _lock.locked():
        return False
    threading.Thread(target=generate, kwargs={"force": force, "what": what},
                     name="coach", daemon=True).start()
    return True


def state() -> dict:
    note = kv_get("coach_note")
    if note and note.get("date") != queries.today().isoformat():
        note = None  # yesterday's note is stale
    review = kv_get("weekly_review")
    last_week = queries.week_start(queries.today()) - timedelta(days=7)
    if review and not review.get("week", "").startswith(last_week.isoformat()):
        review = None  # a review of an older week
    return {"configured": settings.coach_configured, "model": settings.ollama_model or None,
            "url": settings.ollama_url or None, "note": note, "review": review,
            "writing": _lock.locked(), "status": kv_get("coach_status")}
