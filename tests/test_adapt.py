"""The plan follows what was actually run: no confirmations needed."""
from datetime import date, timedelta

import pytest

from app import adapt, plan

PACES = plan.training_paces(45, 300)
MON = date(2026, 10, 5)
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
EASY_Z = [1800, 1200, 300, 0, 0]   # no time in Z4-Z5
HARD_Z = [600, 600, 600, 900, 300]  # 20 min in Z4-Z5


def d(name: str) -> str:
    return (MON + timedelta(days=DAYS.index(name))).isoformat()


def week(**sessions):
    out = [{"date": d(n), "type": k, "title": k.replace("_", " ").title(), "detail": "",
            "distance_km": 16.0 if k == "long" else 8.0} for n, k in sessions.items()]
    w = {"index": 1, "start": MON.isoformat(), "sessions": sorted(out, key=lambda s: s["date"])}
    adapt.apply_overrides([w], {}, PACES)
    return w


def run(day, km=8.0, zones=EASY_Z, rid=None):
    return {"id": rid or DAYS.index(day) + 1, "name": "Run", "date": d(day), "distance_km": km,
            "hr_zones": zones, "splits": None}


def by_type(w):
    return {s["type"]: s for s in w["sessions"]}


def test_classify():
    assert adapt.classify(run("tue"), PACES, 16) == "easy"
    assert adapt.classify(run("tue", zones=HARD_Z), PACES, 16) == "quality"
    assert adapt.classify(run("sun", km=14), PACES, 16) == "long"
    fast = {**run("tue", zones=None), "splits": [{"distance_m": 1000, "pace_s_per_km": PACES["threshold"]}] * 4}
    assert adapt.classify(fast, PACES, 16) == "quality"  # no HR: fast km splits
    assert adapt.classify({**fast, "splits": fast["splits"][:2]}, PACES, 16) == "easy"


def test_on_plan_week_just_ticks_off():
    w = week(tue="threshold", thu="easy", sun="long")
    adapt.reconcile([w], [run("tue", zones=HARD_Z), run("thu")], date(2026, 10, 9), PACES)
    s = by_type(w)
    assert s["threshold"]["status"] == "done" and s["easy"]["status"] == "done"
    assert s["long"]["status"] == "upcoming" and w["changes"] == []


def test_push_day_is_recognised_and_days_swap():
    """Thursday's threshold done on Tuesday instead of the easy run: no click needed."""
    w = week(tue="easy", thu="threshold", sun="long")
    adapt.reconcile([w], [run("tue", zones=HARD_Z)], date(2026, 10, 6), PACES, "good")
    s = by_type(w)
    assert s["threshold"]["date"] == d("tue") and s["threshold"]["status"] == "done"
    assert s["threshold"]["moved_from"] == d("thu")
    assert s["easy"]["date"] == d("thu") and s["easy"]["status"] == "upcoming"
    assert w["changes"] == ["Thursday's threshold done on Tuesday, easy moved to Thursday"]


def test_easy_run_on_a_hard_day_moves_the_workout():
    """Not feeling it: ran easy on Tuesday instead of the threshold."""
    w = week(tue="threshold", thu="easy", sun="long")
    adapt.reconcile([w], [run("tue")], date(2026, 10, 6), PACES, "low")
    s = by_type(w)
    assert s["easy"]["date"] == d("tue") and s["easy"]["status"] == "done"
    assert s["threshold"]["date"] == d("thu") and s["threshold"]["status"] == "upcoming"


def test_displaced_workout_keeps_its_spacing():
    w = week(tue="threshold", fri="easy", sat="intervals", sun="long")
    adapt.reconcile([w], [run("tue")], date(2026, 10, 6), PACES, "low")
    s = by_type(w)
    # the easy run came from Friday, but Friday sits next to Saturday's intervals:
    # the threshold goes to the next day that's clear of hard days instead
    assert s["threshold"]["date"] == d("thu") and s["threshold"]["status"] == "upcoming"


def test_missed_workout_moves_to_a_free_day():
    w = week(mon="threshold", wed="easy", fri="easy", sun="long")
    adapt.reconcile([w], [], date(2026, 10, 7), PACES, "good")
    s = by_type(w)
    assert s["threshold"]["date"] == d("wed") and s["threshold"]["status"] == "today"
    assert w["changes"][0].startswith("Threshold missed on Monday, moved to Wednesday")
    assert [x for x in w["sessions"] if x["type"] == "easy"][0]["status"] == "dropped"  # made room


def test_missed_workout_not_onto_a_day_you_are_not_fresh():
    w = week(mon="threshold", wed="easy", fri="easy", sun="long")
    adapt.reconcile([w], [], date(2026, 10, 7), PACES, "low")
    assert by_type(w)["threshold"]["date"] == d("fri")  # not today or tomorrow, not next to Sunday


def test_missed_workout_let_go_when_no_room():
    w = week(tue="threshold", thu="intervals", sat="easy", sun="long")
    adapt.reconcile([w], [run("wed")], date(2026, 10, 8), PACES, "ok")
    s = by_type(w)
    assert s["threshold"]["status"] == "missed" and "let it go" in w["changes"][-1]


def test_catching_up_a_missed_workout():
    w = week(mon="threshold", wed="easy", fri="easy", sun="long")
    adapt.reconcile([w], [run("wed", zones=HARD_Z)], date(2026, 10, 7), PACES)
    s = by_type(w)
    assert s["threshold"]["date"] == d("wed") and s["threshold"]["status"] == "done"
    dropped = [x for x in w["sessions"] if x["status"] == "dropped"]
    assert len(dropped) == 1 and dropped[0]["note"] == "replaced by threshold"


def test_missed_easy_runs_are_not_made_up():
    w = week(mon="easy", wed="easy", sun="long")
    adapt.reconcile([w], [], date(2026, 10, 8), PACES, "good")
    assert [s["status"] for s in w["sessions"]] == ["missed", "missed", "upcoming"]
    assert w["changes"] == []


def test_double_run_day_joins_that_days_session():
    w = week(tue="easy", thu="easy", sun="long")
    adapt.reconcile([w], [run("tue", km=5, rid=1), run("tue", km=4, rid=2)], date(2026, 10, 6), PACES)
    tue = [s for s in w["sessions"] if s["date"] == d("tue")][0]
    assert len(tue["runs"]) == 2 and tue["status"] == "done"
    assert [s for s in w["sessions"] if s["date"] == d("thu")][0]["status"] == "upcoming"


def test_unplanned_run_is_extra_and_counts_in_the_week():
    w = week(sun="long")
    adapt.reconcile([w], [run("tue")], date(2026, 10, 6), PACES)
    assert [r["id"] for r in w["extra_runs"]] == [2] and w["actual_km"] == 8.0


def test_easy_run_on_a_rest_day_does_not_use_up_another_easy():
    w = week(tue="easy", thu="easy", sat="easy", sun="long")
    adapt.reconcile([w], [run("wed"), run("thu")], date(2026, 10, 8), PACES)
    assert [r["date"] for r in w["extra_runs"]] == [d("wed")] and w["changes"] == []
    assert [s["status"] for s in w["sessions"]] == ["missed", "done", "upcoming", "upcoming"]


def test_long_run_a_day_early_on_a_rest_day():
    w = week(tue="easy", thu="threshold", sun="long")
    adapt.reconcile([w], [run("sat", km=16)], date(2026, 10, 10), PACES)
    long = by_type(w)["long"]
    assert long["date"] == d("sat") and long["status"] == "done"


def test_a_longer_easy_run_is_not_the_long_run():
    w = week(tue="easy", thu="easy", sun="long")  # easy 8 km, long 16 km: long from 12 km
    adapt.reconcile([w], [run("thu", km=11.5)], date(2026, 10, 8), PACES)
    assert by_type(w)["long"]["status"] == "upcoming"


def test_manual_kind_overrides_classification():
    w = week(tue="easy", thu="threshold", sun="long")
    # a hot day pushed HR up on an easy run: tell the app it was easy
    adapt.reconcile([w], [run("tue", zones=HARD_Z, rid=7)], date(2026, 10, 6), PACES, kinds={"7": "easy"})
    s = by_type(w)
    assert s["easy"]["status"] == "done" and s["threshold"]["date"] == d("thu")
    assert s["easy"]["runs"][0]["kind_manual"]


def test_advice_is_information_only():
    w = week(tue="threshold", thu="easy", sun="long")
    today = date(2026, 10, 6)
    adapt.reconcile([w], [], today, PACES, "low")
    [tip] = adapt.advice(w, {"level": "low"}, today, [w])
    assert "the threshold moves to Thursday automatically" in tip
    # and that's what actually happens if the easy run is done
    w2 = week(tue="threshold", thu="easy", sun="long")
    adapt.reconcile([w2], [run("tue")], today, PACES, "low")
    assert by_type(w2)["threshold"]["date"] == d("thu")
    # no room left on a Sunday long run: say so instead of promising a move
    w3 = week(tue="easy", thu="threshold", sun="long")
    sunday = date(2026, 10, 11)
    adapt.reconcile([w3], [run("tue"), run("thu", zones=HARD_Z)], sunday, PACES, "low")
    [tip] = adapt.advice(w3, {"level": "low"}, sunday, [w3])
    assert "no room left this week" in tip and "shorter, easy long run" in tip


def test_push_day_tip():
    today = date(2026, 10, 6)
    w = week(mon="easy", tue="easy", thu="threshold", sun="long")
    adapt.reconcile([w], [], today, PACES, "good")
    [tip] = adapt.advice(w, {"level": "good"}, today, [w])
    assert "Thursday's threshold today" in tip


def test_workout_swapped_into_tomorrow_after_a_low_day_moves_on():
    w = week(tue="threshold", wed="easy", sat="easy", sun="long")
    adapt.reconcile([w], [run("tue")], date(2026, 10, 6), PACES, "low")
    # the easy run came from Wednesday; tomorrow is too soon, Thursday is clear
    assert by_type(w)["threshold"]["date"] == d("thu")


def test_manual_move_still_available_for_planning_ahead():
    w = week(tue="easy", thu="threshold", sat="easy", sun="long")
    today = date(2026, 10, 6)
    adapt.reconcile([w], [], today, PACES)
    ov, warn = adapt.move([w], {}, d("thu"), d("sat"), today)  # next to Sunday's long run
    assert ov == {d("thu"): {"date": d("sat")}, d("sat"): {"date": d("thu")}} and warn
    with pytest.raises(ValueError, match="within their week"):
        adapt.move([w], {}, d("thu"), "2026-10-13", today)
