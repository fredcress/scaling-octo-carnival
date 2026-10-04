from datetime import date, timedelta

import pytest

from app import plan


def cfg(**kw):
    base = {"race_name": "", "race_date": "2027-01-17", "start_date": "2026-10-05",
            "distance_m": 21097.5, "goal_time_s": None, "runs_per_week": 4, "long_run_day": 6,
            "baseline": {"week_km": 22, "long_km": 10, "vdot": 42}}
    return base | kw


def test_vdot_matches_daniels_tables():
    assert plan.vdot(5000, 19 * 60 + 57) == pytest.approx(50, abs=0.2)
    assert plan.race_time(21097.5, 50) == pytest.approx(5495, abs=30)  # 1:31:35
    assert plan.race_time(5000, plan.vdot(5000, 1500)) == pytest.approx(1500, abs=1)


def test_training_paces_vdot_50():
    p = plan.training_paces(50, 300)
    assert p["threshold"] == pytest.approx(255, abs=3)  # 4:15/km
    assert p["interval"] == pytest.approx(235, abs=3)   # 3:55/km
    assert p["easy"][0] < p["easy"][1]                  # fast end first
    assert p["race"] == 300


@pytest.mark.parametrize("distance", list(plan.RACE_DISTANCES))
@pytest.mark.parametrize("runs", [3, 4, 5, 6])
def test_plan_shape(distance, runs):
    p = plan.generate(cfg(distance_m=distance, runs_per_week=runs, race_date="2027-03-14"))
    weeks = p["weeks"]
    assert weeks[0]["start"] == "2026-10-05"
    assert weeks[-1]["phase"] == "race"
    race = weeks[-1]["sessions"][-1]
    assert race["type"] == "race" and race["date"] == "2027-03-14"
    phases = [w["phase"] for w in weeks]
    assert phases == sorted(phases, key=["base", "build", "peak", "taper", "race"].index)
    for w in weeks[:-1]:
        assert len(w["sessions"]) == runs
        days = [s["date"] for s in w["sessions"]]
        assert len(set(days)) == runs and all(w["start"] <= d for d in days)
    # the weekly target grows at most 10 % over the last full (non-recovery) week,
    # and the sessions add up to about the target
    last = None
    for w in weeks:
        if w["phase"] == "race":
            continue
        assert w["planned_km"] <= w["target_km"] * 1.05 + 1, w
        if w["phase"] not in ("base", "build", "peak") or w["recovery"]:
            continue
        if last:
            assert w["target_km"] <= last * plan.WEEKLY_GROWTH + 0.1, w
        last = w["target_km"]
    peak = max(w["planned_km"] for w in weeks)
    taper = [w for w in weeks if w["phase"] == "taper"]
    assert taper and all(w["planned_km"] < peak for w in taper)
    longs = [s["distance_km"] for w in weeks for s in w["sessions"] if s["type"] == "long"]
    assert max(longs) <= plan.PEAK_LONG_KM[distance]


def test_long_run_on_chosen_day():
    p = plan.generate(cfg(long_run_day=5))
    for w in p["weeks"][:-1]:
        for s in w["sessions"]:
            if s["type"] == "long":
                assert date.fromisoformat(s["date"]).weekday() == 5


def test_goal_sets_race_pace_and_no_data_fallback():
    p = plan.generate(cfg(goal_time_s=6300, baseline={"week_km": 0, "long_km": 0, "vdot": None}))
    assert p["paces"]["race"] == round(6300 / 21.0975)
    assert p["vdot_train"] == p["vdot_goal"] and p["predicted_time_s"] is None
    p = plan.generate(cfg(baseline={"week_km": 0, "long_km": 0, "vdot": None}))
    assert p["vdot_train"] == plan.DEFAULT_VDOT


def test_late_week_start_moves_to_monday():
    p = plan.generate(cfg(start_date="2026-10-04"))  # a Sunday
    assert p["weeks"][0]["start"] == "2026-10-05"
    p = plan.generate(cfg(start_date="2026-10-06"))  # a Tuesday: no Wednesday-before sessions
    assert all(s["date"] >= "2026-10-06" for s in p["weeks"][0]["sessions"])


def test_short_plan_is_taper_and_race():
    start = date(2026, 10, 5)
    p = plan.generate(cfg(start_date=start.isoformat(),
                          race_date=(start + timedelta(days=9)).isoformat()))
    assert [w["phase"] for w in p["weeks"]] == ["taper", "race"]
