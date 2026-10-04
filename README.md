# scaling-octo-carnival

A self-hosted running dashboard for a Garmin watch synced through
[Gadgetbridge](https://gadgetbridge.org/). It reads the `.fit` files that
Syncthing drops on the NAS, tracks how your running develops over time, and
pushes any activity to Strava with one click.

```
Garmin FR265 ──BLE──▶ Gadgetbridge (phone) ──Syncthing──▶ NAS folder ──▶ this app ──▶ Strava
```

## Features

- **Dashboard**: this week vs. the same point last week, month and year totals,
  fitness / fatigue / form, 28-day pace trend, the watch's VO₂max, a
  26-week distance chart, and recent activities.
- **Training plan**: pick a race (5 km to marathon), a date and optionally a
  goal time. It builds a week-by-week plan (base, build, peak, taper, race week)
  from your recent volume, longest run and fitness (Daniels VDOT from your best
  efforts). It shows easy, threshold, interval and race paces, says whether the
  goal looks realistic, and marks each session done / missed against your
  actual runs.
- **Activity page**: OpenStreetMap route, pace / HR / elevation / cadence /
  power charts, time in HR zones, km splits, laps, best efforts with PR badges,
  Garmin training effect.
- **Trends**: monthly distance, pace and aerobic efficiency per run with a
  10-run average, weekly time in HR zones, cadence, VO₂max.
- **Records**: fastest 1 km / mile / 5 km / 10 km / half / marathon found
  *inside* any run, top 5 each, plus race predictions.
- **Heatmap** of every run.
- **Push to Strava** button on every activity (runs, rides, walks…), with
  duplicate detection and a link to the Strava activity once uploaded.
- Password login, light/dark theme from the OS, works on a phone.

All activities are stored and can be pushed to Strava. **KPIs, charts, records
and the heatmap count runs only** (`sport = running`, including trail and
treadmill).

## Setup

### 1. Phone → NAS

1. In Gadgetbridge, make sure activity `.fit` files are written to a folder on
   the phone.
2. Share that folder with your TrueNAS using Syncthing. Note its path on the
   NAS, e.g. `/mnt/tank/syncthing/gadgetbridge`.

The app scans the folder recursively every `SCAN_INTERVAL` seconds. Files that
aren't activities (monitoring, sleep, settings…) are skipped and shown in
Settings → Import log. Each imported file is copied into the data folder, so
deleting it from the phone later is fine.

### 2. Strava API app

1. Go to <https://www.strava.com/settings/api> and create an application.
2. **Authorization Callback Domain**: the host you open the dashboard with,
   without scheme or port (e.g. `truenas.local` or `192.168.1.20`).
3. Copy the **Client ID** and **Client Secret** into `.env`.
4. After the app is running: **Settings → Connect Strava**, then approve
   (keep "Upload your activities" ticked).

> **If Strava refuses your callback domain**, get a refresh token by hand and
> set `STRAVA_REFRESH_TOKEN` instead:
> 1. Set the callback domain to `localhost`.
> 2. Open
>    `https://www.strava.com/oauth/authorize?client_id=ID&response_type=code&redirect_uri=http://localhost&scope=read,activity:write&approval_prompt=force`
> 3. Copy `code=` from the URL you are redirected to (the page won't load,
>    which is fine).
> 4. Run
>    `curl -X POST https://www.strava.com/oauth/token -d client_id=ID -d client_secret=SECRET -d code=CODE -d grant_type=authorization_code`
>    and copy `refresh_token` from the response.

### 3. Image

Pushing to `main` runs the tests, then GitHub Actions builds and publishes
`ghcr.io/<owner>/scaling-octo-carnival:latest`. GHCR packages are private by
default. Either make the package public (GitHub → Packages → Package settings →
Change visibility), or run `docker login ghcr.io` on the NAS with a token that
has `read:packages`.

### 4. Dockge

1. Create a stack and paste in `compose.yaml`.
2. Create `.env` from `.env.example` and fill in at least `GHCR_OWNER`,
   `DATA_PATH`, `FIT_PATH`, `APP_PASSWORD`, `MAX_HR`, `RESTING_HR`, the Strava
   values and `PUBLIC_URL`.
3. Create `DATA_PATH` and make it writable by `PUID:PGID` (568:568 is TrueNAS's
   `apps` user).
4. Deploy, then open `http://<nas>:8420`.

If you put it behind a reverse proxy with HTTPS, set
`PUBLIC_URL=https://runs.example.com` and `SESSION_HTTPS_ONLY=true`.

## Configuration

| Variable | Default | |
|---|---|---|
| `APP_USERNAME` / `APP_PASSWORD` | `admin` / — | Login. The password is required. |
| `SECRET_KEY` | generated | Signs the session cookie. If unset, one is generated and kept in `/data`. |
| `MAX_HR`, `RESTING_HR` | 190, 50 | HR zones and training load. |
| `SEX` | `male` | Picks the TRIMP weighting. |
| `TZ` | `UTC` | Only used when a FIT file has no local time. |
| `SCAN_INTERVAL` | 60 | Seconds between folder scans. |
| `STRAVA_CLIENT_ID` / `STRAVA_CLIENT_SECRET` | — | From your Strava API app. |
| `PUBLIC_URL` | request URL | Base URL for the Strava OAuth callback. |
| `STRAVA_REFRESH_TOKEN` | — | Optional alternative to the Connect button. |

After changing `MAX_HR` or `RESTING_HR`: restart, then **Settings →
Recalculate all**.

## How the numbers are computed

- **HR zones**: Karvonen (heart-rate reserve). Z1 50–60 %, Z2 60–70 %,
  Z3 70–80 %, Z4 80–90 %, Z5 90 %+.
- **Load**: Banister TRIMP, summed sample by sample. Runs without HR get an
  estimate (marked `*` / "est.").
- **Fitness / fatigue / form**: 42-day and 7-day exponentially weighted load
  (CTL / ATL). Form is yesterday's fitness minus fatigue.
- **Aerobic efficiency**: average speed per heartbeat, in metres per beat.
  Rising means fitter.
- **Best efforts**: the fastest window covering each distance, using the
  watch's distance stream.
- **Race predictions**: Riegel (`T2 = T1 × (D2/D1)^1.06`) from best efforts of
  the last 90 days, using ≥ 5 km efforts when there are any.
- **VO₂max**: read on a best-effort basis from Garmin's undocumented
  physiological-metrics message. It shows "–" if your files don't include it.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest

# try it with ~6 months of synthetic activities
python scripts/make_demo_data.py demo-watch --days 200
DATA_DIR=./data WATCH_DIR=./demo-watch APP_PASSWORD=dev uvicorn app.main:app --reload
```

Stack: FastAPI, SQLite, fitdecode, Chart.js, and Leaflet with leaflet.heat.
The frontend libraries are vendored in `app/static/vendor`, so there's no
build step.

## Roadmap

- Daily health data (steps, resting HR, HRV, sleep) from Gadgetbridge's
  auto-exported database, shown next to the fitness chart.
