import hmac
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from . import analytics as an
from . import plan, queries, strava, watcher
from .config import settings
from .db import connect, init_db, kv_delete, kv_get, kv_set
from .fitimport import reprocess

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if not settings.password:
        raise RuntimeError("APP_PASSWORD must be set")
    init_db()
    # uploads interrupted by a restart would otherwise show "uploading" forever
    with connect() as conn:
        conn.execute("UPDATE activities SET strava_status = 'error', "
                     "strava_error = 'interrupted by restart' WHERE strava_status = 'uploading'")
    watcher.start()
    yield


app = FastAPI(title="scaling-octo-carnival", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="soc_session",
                   max_age=30 * 24 * 3600, same_site="lax", https_only=settings.https_only)


# ------------------------------------------------------------------ auth

def require_user(request: Request) -> str:
    user = request.session.get("user")
    if not user:
        raise HTTPException(401, "login required")
    if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "fetch":
        raise HTTPException(403, "missing X-Requested-With header")
    return user


@app.get("/login")
def login_page():
    return FileResponse(STATIC / "login.html")


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    ok = (hmac.compare_digest(username.encode(), settings.username.encode())
          & hmac.compare_digest(password.encode(), settings.password.encode()))
    if not ok:
        time.sleep(1)  # slow down guessing
        return RedirectResponse("/login?error=1", status_code=303)
    request.session["user"] = username
    return RedirectResponse("/", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ------------------------------------------------------------------ API

api = Depends(require_user)


@app.get("/api/summary", dependencies=[api])
def api_summary():
    return queries.summary()


@app.get("/api/activities", dependencies=[api])
def api_activities(kind: str = "all"):
    return queries.activities(kind)


@app.get("/api/activities/{activity_id}", dependencies=[api])
def api_activity(activity_id: int):
    a = queries.activity(activity_id)
    if not a:
        raise HTTPException(404, "activity not found")
    return a


@app.get("/api/activities/{activity_id}/strava", dependencies=[api])
def api_strava_state(activity_id: int):
    with connect() as conn:
        r = conn.execute("SELECT strava_status, strava_activity_id, strava_error "
                         "FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if not r:
        raise HTTPException(404, "activity not found")
    return dict(r)


@app.post("/api/activities/{activity_id}/strava", dependencies=[api])
def api_strava_push(activity_id: int, background: BackgroundTasks):
    with connect() as conn:
        r = conn.execute("SELECT strava_status FROM activities WHERE id = ?",
                         (activity_id,)).fetchone()
        if not r:
            raise HTTPException(404, "activity not found")
        if r["strava_status"] in ("uploading", "done", "duplicate"):
            raise HTTPException(409, f"already {r['strava_status']}")
        if not strava.status()["connected"]:
            raise HTTPException(400, "Strava is not connected - see Settings")
        conn.execute("UPDATE activities SET strava_status = 'uploading', strava_error = NULL "
                     "WHERE id = ?", (activity_id,))
    background.add_task(strava.upload, activity_id)
    return {"strava_status": "uploading"}


@app.get("/api/trends", dependencies=[api])
def api_trends():
    return queries.trends()


@app.get("/api/records", dependencies=[api])
def api_records():
    return queries.records()


@app.get("/api/heatmap", dependencies=[api])
def api_heatmap():
    return queries.heatmap()


class PlanIn(BaseModel):
    race_name: str = Field("", max_length=80)
    race_date: date
    distance_m: float
    goal_time_s: float | None = Field(None, gt=0)
    runs_per_week: int = Field(4, ge=3, le=6)
    long_run_day: int = Field(6, ge=0, le=6)


def _plan_response() -> dict:
    cfg = kv_get("plan")
    return {"plan": queries.plan_view(cfg) if cfg else None, "baseline": queries.plan_baseline()}


@app.get("/api/plan", dependencies=[api])
def api_plan():
    return _plan_response()


@app.post("/api/plan", dependencies=[api])
def api_plan_save(body: PlanIn):
    t = queries.today()
    if body.distance_m not in plan.RACE_DISTANCES:
        raise HTTPException(400, "unsupported race distance")
    if not t + timedelta(days=7) <= body.race_date <= t + timedelta(days=365):
        raise HTTPException(400, "race date must be between 1 week and 1 year from today")
    if body.goal_time_s and not 120 <= body.goal_time_s / body.distance_m * 1000 <= 900:
        raise HTTPException(400, "goal time works out at an unrealistic pace")
    # saving (re)builds from today and from current fitness, so paces stay honest
    kv_set("plan", {**body.model_dump(mode="json"), "start_date": t.isoformat(),
                    "baseline": queries.plan_baseline()})
    return _plan_response()


@app.delete("/api/plan", dependencies=[api])
def api_plan_delete():
    kv_delete("plan")
    return _plan_response()


@app.get("/api/settings", dependencies=[api])
def api_settings():
    return {
        "watch_dir": str(settings.watch_dir),
        "watch_dir_exists": settings.watch_dir.is_dir(),
        "scan_interval": settings.scan_interval,
        "max_hr": settings.max_hr,
        "resting_hr": settings.resting_hr,
        "sex": settings.sex,
        "tz": settings.tz,
        "zone_limits": an.hr_zone_limits(settings.max_hr, settings.resting_hr),
        "strava": strava.status(),
        "last_scan": kv_get("last_scan"),
        "reprocess": kv_get("reprocess"),
        "import_log": queries.import_log(),
    }


@app.post("/api/scan", dependencies=[api])
def api_scan():
    return watcher.scan_once()


_reprocess_lock = threading.Lock()


def _reprocess_all() -> None:
    with _reprocess_lock:
        with connect() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM activities")]
        failed = 0
        for i, activity_id in enumerate(ids):
            try:
                reprocess(activity_id)
            except Exception:
                log.exception("reprocess of %s failed", activity_id)
                failed += 1
            kv_set("reprocess", {"done": i + 1, "total": len(ids), "failed": failed})


@app.post("/api/reprocess", dependencies=[api])
def api_reprocess(background: BackgroundTasks):
    if _reprocess_lock.locked():
        raise HTTPException(409, "already running")
    background.add_task(_reprocess_all)
    return {"started": True}


# ------------------------------------------------------------------ Strava OAuth

def _redirect_uri(request: Request) -> str:
    base = settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/strava/callback"


@app.get("/strava/connect")
def strava_connect(request: Request):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    if not settings.strava_configured:
        raise HTTPException(400, "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in .env first")
    state = secrets.token_urlsafe(16)
    request.session["strava_state"] = state
    return RedirectResponse(strava.authorize_url(_redirect_uri(request), state), status_code=303)


@app.get("/strava/callback")
def strava_callback(request: Request, state: str = "", code: str = "", error: str = "",
                    scope: str = ""):
    if not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    expected = request.session.pop("strava_state", None)
    if error or not code or not expected or not hmac.compare_digest(state, expected):
        return RedirectResponse("/#/settings?strava=denied", status_code=303)
    if "activity:write" not in scope.split(","):
        return RedirectResponse("/#/settings?strava=scope", status_code=303)
    try:
        strava.exchange_code(code)
    except strava.StravaError as e:
        log.warning("%s", e)
        return RedirectResponse("/#/settings?strava=failed", status_code=303)
    return RedirectResponse("/#/settings?strava=connected", status_code=303)


@app.post("/api/strava/disconnect", dependencies=[api])
def api_strava_disconnect():
    strava.disconnect()
    return strava.status()

