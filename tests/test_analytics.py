from datetime import date

from app import analytics as an


def test_zone_limits_karvonen():
    assert an.hr_zone_limits(190, 50) == [120, 134, 148, 162, 176]


def test_zone_of():
    limits = an.hr_zone_limits(190, 50)
    assert an.zone_of(100, limits) == 0  # below Z1 counts as Z1
    assert an.zone_of(150, limits) == 2
    assert an.zone_of(189, limits) == 4


def test_time_in_zones_caps_pauses():
    limits = an.hr_zone_limits(190, 50)
    t = [0, 1, 2, 300]  # 298 s pause before the last sample
    hr = [150, 150, 150, 150]
    zones = an.time_in_zones(t, hr, limits)
    assert zones[2] == 1 + 1 + an.MAX_GAP_S


def test_trimp_none_without_hr():
    assert an.trimp([0, 1], [None, None], 190, 50, "male") is None


def test_best_efforts_constant_pace():
    # 4 m/s for 3000 s -> 12 km; 1 km should take 250 s
    t = list(range(3001))
    d = [4.0 * x for x in t]
    eff = an.best_efforts(t, d)
    assert eff[1000][0] == 250
    assert eff[10000][0] == 2500
    assert 21097.5 not in eff


def test_best_efforts_finds_fast_segment():
    t, d, dist = [], [], 0.0
    for s in range(2000):
        t.append(s)
        d.append(dist)
        dist += 5.0 if 1000 <= s < 1200 else 3.0  # 1 km at 5 m/s in the middle
    eff = an.best_efforts(t, d)
    assert abs(eff[1000][0] - 200) < 1


def test_km_splits():
    t = list(range(0, 1101))
    d = [x * 2.0 for x in t]  # 2.2 km at 2 m/s
    hr = [140] * len(t)
    alt = [100] * len(t)
    splits = an.km_splits(t, d, hr, alt)
    assert [s["km"] for s in splits] == [1, 2, 3]
    assert splits[0]["pace_s_per_km"] == 500
    assert abs(splits[2]["distance_m"] - 200) < 1


def test_fitness_series_rises_and_form_lags():
    load = {"2026-01-01": 100.0}
    s = an.fitness_series(load, date(2026, 1, 1), date(2026, 1, 3))
    assert s[0]["ctl"] > 0 and s[0]["atl"] > s[0]["ctl"]
    assert s[0]["tsb"] == 0  # form uses yesterday's values
    assert s[1]["tsb"] < 0


def test_riegel():
    assert round(an.riegel(1200, 5000, 10000)) == 2502
