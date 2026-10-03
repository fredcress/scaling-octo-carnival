"""Generate realistic synthetic FIT activities for trying the dashboard without a watch.

    pip install -r requirements-dev.txt
    python scripts/make_demo_data.py ./demo-watch --days 240

Point WATCH_DIR at the output folder. Uses fit-tool (dev dependency only)."""
import argparse
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import FileType, Manufacturer, Sport, SubSport

MAX_HR, REST_HR = 190, 50
# a few loops around different start points, so the heatmap has something to show
ROUTES = [(45.7640, 4.8357, 1.0), (45.7710, 4.8530, 0.8), (45.7485, 4.8460, 1.3),
          (45.7790, 4.8150, 1.1)]


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def write_activity(path: Path, start: datetime, sport: Sport, sub: SubSport,
                   distance_km: float, pace_s: float, hr_frac: float,
                   intervals: bool = False, gps: bool = True, tz_offset_h: int = 2,
                   seed: int = 0) -> None:
    rnd = random.Random(seed)
    lat0, lon0, scale = rnd.choice(ROUTES)
    radius_m = distance_km * 1000 / (2 * math.pi) * scale
    records = []
    t = 0.0
    dist = 0.0
    hr = REST_HR + 20.0
    target = distance_km * 1000
    while dist < target:
        frac = dist / target
        speed = 1000 / pace_s
        effort = hr_frac
        if intervals and 0.2 < frac < 0.8:
            on = int((frac - 0.2) / 0.6 * 12) % 2 == 0
            speed *= 1.22 if on else 0.8
            effort = hr_frac + (0.12 if on else -0.04)
        speed *= 1 + rnd.gauss(0, 0.03)
        effort += frac * 0.04  # cardiac drift
        target_hr = REST_HR + (MAX_HR - REST_HR) * min(effort, 0.98)
        hr += (target_hr - hr) * 0.08 + rnd.gauss(0, 0.8)
        ang = dist / (radius_m * 2 * math.pi) * 2 * math.pi
        lat = lat0 + (radius_m * math.sin(ang) * 1.3) / 111_320
        lon = lon0 + (radius_m * (1 - math.cos(ang))) / (111_320 * math.cos(math.radians(lat0)))
        alt = 180 + 25 * math.sin(ang * 3) + 8 * math.sin(ang * 11)
        records.append((t, dist, speed, hr, alt, lat, lon))
        t += 1
        dist += speed

    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    fid.product = 4257
    fid.serial_number = 3412345678
    fid.time_created = ms(start)

    b = FitFileBuilder(auto_define=True, min_string_size=50)
    b.add(fid)
    cadence = 84 + 1000 / pace_s * 3 if sport == Sport.RUNNING else 0
    for (t, d, sp, h, alt, lat, lon) in records:
        r = RecordMessage()
        r.timestamp = ms(start + timedelta(seconds=t))
        r.distance = d
        r.enhanced_speed = sp
        r.heart_rate = int(h)
        r.enhanced_altitude = alt
        if sport == Sport.RUNNING:
            r.cadence = int(cadence + rnd.gauss(0, 1))
            r.power = int(sp * 75 + rnd.gauss(0, 6))
        if gps:
            r.position_lat = lat
            r.position_long = lon
        b.add(r)

    total_t = records[-1][0]
    total_d = records[-1][1]
    end = start + timedelta(seconds=total_t)
    # auto-lap every km
    lap_start = 0
    for i in range(1, len(records)):
        if records[i][1] // 1000 > records[i - 1][1] // 1000 or i == len(records) - 1:
            seg = records[lap_start:i + 1]
            lap = LapMessage()
            lap.timestamp = ms(start + timedelta(seconds=seg[-1][0]))
            lap.start_time = ms(start + timedelta(seconds=seg[0][0]))
            lap.total_elapsed_time = seg[-1][0] - seg[0][0]
            lap.total_timer_time = seg[-1][0] - seg[0][0]
            lap.total_distance = seg[-1][1] - seg[0][1]
            lap.avg_heart_rate = int(sum(x[3] for x in seg) / len(seg))
            lap.max_heart_rate = int(max(x[3] for x in seg))
            b.add(lap)
            lap_start = i

    s = SessionMessage()
    s.timestamp = ms(end)
    s.start_time = ms(start)
    s.sport = sport
    s.sub_sport = sub
    s.total_elapsed_time = total_t
    s.total_timer_time = total_t
    s.total_distance = total_d
    s.enhanced_avg_speed = total_d / total_t
    s.enhanced_max_speed = max(x[2] for x in records)
    s.avg_heart_rate = int(sum(x[3] for x in records) / len(records))
    s.max_heart_rate = int(max(x[3] for x in records))
    s.total_ascent = int(sum(max(0, records[i][4] - records[i - 1][4]) for i in range(1, len(records))))
    s.total_descent = s.total_ascent
    s.total_calories = int(total_d / 1000 * 70)
    if sport == Sport.RUNNING:
        s.avg_cadence = int(cadence)
        s.avg_power = int(total_d / total_t * 75)
        s.total_training_effect = round(min(5.0, 1.5 + hr_frac * 3 + total_t / 7200), 1)
        s.total_anaerobic_training_effect = 2.8 if intervals else 0.6
    b.add(s)

    a = ActivityMessage()
    a.timestamp = ms(end)
    a.local_timestamp = int((end + timedelta(hours=tz_offset_h)).timestamp()
                            - datetime(1989, 12, 31, tzinfo=timezone.utc).timestamp())
    a.total_timer_time = total_t
    a.num_sessions = 1
    b.add(a)
    b.build().to_file(str(path))


def generate(out: Path, days: int, seed: int = 42) -> int:
    rnd = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    n = 0
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        progress = 1 - back / days  # 0 -> 1, the runner gets fitter
        wd = day.weekday()
        start = day + timedelta(hours=rnd.choice([5, 6, 11, 16, 17]), minutes=rnd.randint(0, 59))
        kind = None
        if wd == 6:
            kind = ("long", 12 + 10 * progress + rnd.uniform(-1, 1), 345 - 25 * progress, 0.66)
        elif wd == 1:
            kind = ("intervals", 8 + rnd.uniform(0, 2), 320 - 30 * progress, 0.72)
        elif wd in (3, 5) or (wd == 4 and rnd.random() < 0.4):
            kind = ("easy", 6 + rnd.uniform(0, 4) + 2 * progress, 350 - 22 * progress, 0.64)
        elif wd == 2 and rnd.random() < 0.5:
            kind = ("tempo", 9 + rnd.uniform(0, 3), 300 - 22 * progress, 0.8)
        if back % 23 == 0:
            kind = None  # rest / missed days
        if kind:
            name, km, pace, eff = kind
            sub = SubSport.TRAIL if rnd.random() < 0.08 else SubSport.GENERIC
            write_activity(out / f"{start:%Y-%m-%d-%H-%M-%S}-run.fit", start, Sport.RUNNING,
                           sub, km, pace, eff, intervals=name == "intervals", seed=n)
            n += 1
        if wd in (0, 5) and rnd.random() < 0.4:
            other = start + timedelta(hours=3)
            if rnd.random() < 0.5:
                write_activity(out / f"{other:%Y-%m-%d-%H-%M-%S}-walk.fit", other, Sport.WALKING,
                               SubSport.GENERIC, rnd.uniform(3, 6), 660, 0.35, seed=n)
            else:
                write_activity(out / f"{other:%Y-%m-%d-%H-%M-%S}-ride.fit", other, Sport.CYCLING,
                               SubSport.ROAD, rnd.uniform(20, 40), 120, 0.6, seed=n)
            n += 1
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--days", type=int, default=240)
    args = ap.parse_args()
    print(f"wrote {generate(args.out, args.days)} activities to {args.out}")
