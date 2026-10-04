"""Adapting the training plan to what actually happens.

The generated plan never changes. On top of it, in this order:
1. Manual adjustments (optional planning ahead), keyed by a session's original date:
     {"date": "2026-10-08"}  moved to another day (same week)
     {"easy": true}          turned into an easy run
     {"skip": true}          let go
2. Reconciliation with the runs actually recorded. Each run is classified as
   easy, hard ("quality") or long and fulfils a planned session of that kind in
   its week, preferably the one planned that day. Doing a session on another day
   moves it there and swaps the displaced session into the freed slot. Missed
   hard sessions move to a free day later in the week, never next to another
   hard day, or are let go; missed easy runs are not made up.

Nothing needs confirming: the watch says what was done, the plan follows.
"""
from datetime import date, timedelta

from . import plan

HARD = {"threshold", "intervals", "race_pace", "fartlek", "long", "race"}
KIND = {"easy": "easy", "long": "long", "race": "race",
        "threshold": "quality", "intervals": "quality", "race_pace": "quality", "fartlek": "quality"}
# which planned session a run of each kind may fulfil, in order of preference
FULFILS = {"long": ("long", "easy"), "quality": ("quality", "easy"), "easy": ("easy",),
           "race": ("race", "long", "quality", "easy")}
HARD_ZONE_S = 600     # 10 min in Z4-Z5 makes a run a workout
FAST_KM = 3.0         # without HR: km splits at threshold pace (+15 s) to count as a workout
LONG_SHARE = 0.75     # of the week's planned long run


def is_hard(s: dict) -> bool:
    return s["type"] in HARD and not s.get("skipped") and s.get("status") != "dropped"


# ---------------------------------------------------------------- manual adjustments

def apply_overrides(weeks: list[dict], overrides: dict, paces: dict) -> None:
    for w in weeks:
        for s in w["sessions"]:
            s["id"] = s["date"]
            o = overrides.get(s["id"])
            if not o:
                continue
            if o.get("easy") and s["type"] != "race":
                was = s["title"]
                s.update(plan.easy_session(date.fromisoformat(s["date"]), min(s["distance_km"], 8), paces))
                s["id"] = s["date"]
                s["changed"] = f"easy instead of {was.lower()}"
            if o.get("date"):
                s["moved_from"] = s["date"]
                s["date"] = o["date"]
            if o.get("skip"):
                s["skipped"] = True
        w["sessions"].sort(key=lambda s: s["date"])


def _days(week: dict) -> list[str]:
    ws = date.fromisoformat(week["start"])
    return [(ws + timedelta(days=i)).isoformat() for i in range(7)]


def spacing_ok(weeks: list[dict], session: dict, day: str, ran_hard: set = frozenset()) -> bool:
    """Would a hard session on `day` sit next to another hard day?"""
    if not is_hard(session):
        return True
    d = date.fromisoformat(day)
    near = {(d + timedelta(days=k)).isoformat() for k in (-1, 0, 1)}
    hard = {s["date"] for w in weeks for s in w["sessions"]
            if is_hard(s) and s["id"] != session["id"] and s.get("status") != "missed"} | set(ran_hard)
    occupant = next((s for w in weeks for s in w["sessions"]
                     if s["date"] == day and s["id"] != session["id"]), None)
    if occupant and is_hard(occupant) and not occupant.get("runs"):
        hard.discard(day)  # it swaps into this session's slot
    return not (near & hard)


def move(weeks: list[dict], overrides: dict, session_id: str, to: str, today: date) -> tuple[dict, list[str]]:
    """New manual overrides after moving a session (swapping with whatever is on `to`)."""
    s = next((x for w in weeks for x in w["sessions"] if x["id"] == session_id), None)
    if not s:
        raise ValueError("no such session")
    week = next(w for w in weeks if s in w["sessions"])
    if to not in _days(week):
        raise ValueError("sessions can only move within their week")
    if to < today.isoformat():
        raise ValueError("can't move a session into the past")
    if s.get("status") == "done":
        raise ValueError("that session is already done")
    warnings = [] if spacing_ok(weeks, s, to) else ["This puts two hard days back to back."]
    out = {k: dict(v) for k, v in overrides.items()}
    occupant = next((x for x in week["sessions"] if x["date"] == to and x["id"] != session_id), None)
    if occupant:
        if occupant.get("status") == "done":
            raise ValueError("there's already a completed session on that day")
        _set_date(out, occupant["id"], s["date"])
    _set_date(out, session_id, to)
    return out, warnings


def _set_date(overrides: dict, session_id: str, day: str) -> None:
    o = overrides.setdefault(session_id, {})
    if day == session_id:
        o.pop("date", None)
    else:
        o["date"] = day
    if not o:
        overrides.pop(session_id)


# ---------------------------------------------------------------- what was actually run

def classify(run: dict, paces: dict, long_km: float, race: dict | None = None,
             easy_km: float | None = None) -> str:
    """easy | quality | long | race, from distance, HR zones or km splits."""
    km = run["distance_km"]
    if race and run["date"] == race["date"] and km >= 0.9 * race["distance_km"]:
        return "race"
    # long = closer to the week's long run than to its usual easy run
    long_from = (easy_km + long_km) / 2 if easy_km else LONG_SHARE * long_km
    if km >= max(long_from, 10):
        return "long"
    zones = run.get("hr_zones")
    if zones:
        return "quality" if sum(zones[3:]) >= HARD_ZONE_S else "easy"
    fast = sum(sp["distance_m"] for sp in run.get("splits") or []
               if sp.get("pace_s_per_km") and sp["pace_s_per_km"] <= paces["threshold"] + 15) / 1000
    return "quality" if fast >= FAST_KM else "easy"


def _day(d: str) -> str:
    return date.fromisoformat(d).strftime("%A")


def free_slot(weeks: list[dict], week: dict, s: dict, today: date, level: str | None,
              ran_today: bool, ran_hard: set = frozenset()) -> tuple[str, dict | None] | None:
    """The first day from today on, in `week`, where hard session `s` can go:
    no other workout or completed run there, not next to a hard day, and not
    on a day you're not fresh for. Returns (day, easy session it would replace)."""
    t = today.isoformat()
    live = [x for x in week["sessions"] if not x.get("skipped") and x.get("status") != "dropped"]
    for d in sorted(x for x in _days(week) if x >= t):
        occ = next((x for x in live if x["date"] == d and x["id"] != s["id"]), None)
        if occ and (is_hard(occ) or occ.get("runs")):
            continue
        if d == t and (ran_today or level in ("ok", "low")):
            continue  # not onto a day you're not fresh for, or already ran
        if level == "low" and d == (today + timedelta(days=1)).isoformat():
            continue  # one night rarely fixes low HRV: leave a day
        if spacing_ok(weeks, s, d, ran_hard):
            return d, occ
    return None


def reconcile(weeks: list[dict], runs: list[dict], today: date, paces: dict,
              level: str | None = None, kinds: dict | None = None) -> None:
    """Match recorded runs to planned sessions and re-plan the rest of each week.
    Sets on each week: sessions' runs/status/date, extra_runs, actual_km, changes."""
    t = today.isoformat()
    kinds = kinds or {}
    for w in weeks:
        days = set(_days(w))
        live = [s for s in w["sessions"] if not s.get("skipped")]
        for s in w["sessions"]:
            s["runs"] = []
            if s.get("skipped"):
                s["status"] = "skipped"
        long_km = max((s["distance_km"] for s in live if s["type"] == "long"), default=20)
        easy_km = max((s["distance_km"] for s in live if s["type"] == "easy"), default=None)
        race = next((s for s in live if s["type"] == "race"), None)
        changes, extra = [], []
        done = set()

        for r in sorted((r for r in runs if r["date"] in days), key=lambda r: (r["date"], r["id"])):
            r["kind"] = kinds.get(str(r["id"])) or classify(r, paces, long_km, race, easy_km)
            r["kind_manual"] = str(r["id"]) in kinds
            # a second run on a day already covered (double day, warm-up file) joins that session
            same_day = next((s for s in live if s["date"] == r["date"] and s["id"] in done), None)
            if same_day:
                same_day["runs"].append(r)
                continue
            target = None
            planned_that_day = any(s["date"] == r["date"] and s.get("status") != "dropped" for s in live)
            for kind in FULFILS[r["kind"]]:
                cands = [s for s in live if s["id"] not in done and KIND[s["type"]] == kind
                         and s.get("status") != "dropped"]
                if kind == "easy" and not planned_that_day:
                    # an extra easy run on a rest day doesn't use up another day's easy run;
                    # it only stands in for one when it replaced what was planned that day
                    cands = []
                if not cands:
                    continue
                target = (next((s for s in cands if s["date"] == r["date"]), None)
                          or next((s for s in reversed(cands) if s["date"] < r["date"]), None)  # catching up
                          or next((s for s in cands if s["date"] > r["date"]), None))          # done early
                break
            if not target:
                extra.append(r)
                continue
            done.add(target["id"])
            target["runs"].append(r)
            if target["date"] == r["date"]:
                continue
            orig = target["date"]
            occupant = next((s for s in live if s["date"] == r["date"] and s["id"] not in done
                             and s.get("status") != "dropped"), None)
            target["moved_from"] = target.get("moved_from") or orig
            target["date"] = r["date"]
            if orig > r["date"]:
                note = f"{_day(orig)}'s {target['title'].lower()} done on {_day(r['date'])}"
            else:
                note = f"{target['title']} from {_day(orig)} made up on {_day(r['date'])}"
            if occupant:
                if orig > t:
                    occupant["moved_from"] = occupant.get("moved_from") or occupant["date"]
                    occupant["date"] = orig
                    note += f", {occupant['title'].lower()} moved to {_day(orig)}"
                    if is_hard(occupant):
                        occupant["needs_slot"] = True  # spacing checked below
                else:
                    occupant["status"] = "dropped"
                    occupant["note"] = f"replaced by {target['title'].lower()}"
                    note += f", replacing that day's {occupant['title'].lower()}"
            changes.append(note)

        def set_status(s):
            if s.get("skipped") or s.get("status") == "dropped":
                return
            km = sum(r["distance_km"] for r in s["runs"])
            s["status"] = ("done" if s["runs"] and km >= 0.6 * s["distance_km"] else
                           "partial" if s["runs"] else
                           "missed" if s["date"] < t else "today" if s["date"] == t else "upcoming")

        for s in live:
            set_status(s)

        ran_hard = {r["date"] for r in extra if r["kind"] in ("quality", "long", "race")}
        ran_today = any(r["date"] == t for r in runs)
        for s in sorted(live, key=lambda s: s["date"]):
            displaced = s.pop("needs_slot", False)
            too_soon = level == "low" and s["date"] == (today + timedelta(days=1)).isoformat()
            if displaced and spacing_ok(weeks, s, s["date"], ran_hard) and not too_soon:
                continue
            if not displaced and not (s["status"] == "missed" and is_hard(s) and s["type"] != "race"):
                continue
            slot = free_slot(weeks, w, s, today, level, ran_today, ran_hard)
            if slot:
                d, occ = slot
                orig = s["date"]
                s["moved_from"] = s.get("moved_from") or orig
                s["date"] = d
                if occ:
                    occ["status"] = "dropped"
                    occ["note"] = f"made room for {s['title'].lower()}"
                set_status(s)
                what = f"{s['title']} moved to {_day(d)}" if displaced \
                    else f"{s['title']} missed on {_day(orig)}, moved to {_day(d)}"
                changes.append(what + (f" in place of the {occ['title'].lower()}" if occ else ""))
            elif s["status"] == "missed":
                s["note"] = "no free day this week without back-to-back hard days"
                changes.append(f"{s['title']} missed on {_day(s['date'])}: no free day left without two hard "
                               "days in a row, so let it go")
            else:
                s["status"] = "dropped"
                s["note"] = "no room left this week"
                changes.append(f"{s['title']} dropped: no room left this week")

        w["sessions"].sort(key=lambda s: s["date"])
        w["extra_runs"] = extra
        w["actual_km"] = round(sum(r["distance_km"] for r in runs if r["date"] in days), 1)
        w["changes"] = changes


def advice(week: dict | None, readiness: dict | None, today: date, weeks: list[dict]) -> list[str]:
    """Readiness tips for today. No buttons: whatever you run, the plan adjusts."""
    if not week or not readiness:
        return []
    t = today.isoformat()
    todays = next((s for s in week["sessions"] if s["date"] == t and s.get("status") in ("today", "upcoming")), None)
    if readiness["level"] == "low" and todays and is_hard(todays) and todays["type"] != "race":
        name = todays["title"].lower()
        # would it fit later this week? (tomorrow's slot excluded: still recovering)
        later = free_slot(weeks, week, todays, today, "low", ran_today=True)
        if later:
            then = f"the {name} moves to {_day(later[0])} automatically"
        elif todays["type"] == "long":
            then = "there's no room left this week to move it, so a shorter, easy long run is the better option"
        else:
            then = f"there's no room left this week, so the {name} is let go; don't cram it in"
        return [f"Not well recovered: an easy run or rest is the better call today. If you run easy, {then}."]
    if readiness["level"] == "good" and not (todays and is_hard(todays)) \
            and not any(r["date"] == t for s in week["sessions"] for r in s["runs"]):
        nxt = next((s for s in week["sessions"] if s["date"] > t and is_hard(s)
                    and s["type"] not in ("race", "long") and s.get("status") == "upcoming"), None)
        if nxt and spacing_ok(weeks, nxt, t):
            return [f"Recovered and feeling good? You could do {_day(nxt['date'])}'s "
                    f"{nxt['title'].lower()} today; the plan will swap the days for you."]
    return []
