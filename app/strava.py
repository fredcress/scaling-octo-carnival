"""Strava OAuth + upload of the original FIT file.

Tokens live in the kv table. STRAVA_REFRESH_TOKEN in .env is an optional
bootstrap for people who prefer to skip the in-app "Connect" flow."""
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .config import settings
from .db import connect, kv_delete, kv_get, kv_set

log = logging.getLogger(__name__)

AUTH_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API = "https://www.strava.com/api/v3"
SCOPE = "read,activity:write"
POLL_TIMEOUT_S = 120


class StravaError(Exception):
    pass


def authorize_url(redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.strava_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": state,
    })


def _store_tokens(data: dict) -> None:
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
    }
    athlete = data.get("athlete")
    if athlete:
        tokens["athlete"] = {
            "id": athlete.get("id"),
            "name": f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip(),
        }
    else:
        old = kv_get("strava_tokens") or {}
        if "athlete" in old:
            tokens["athlete"] = old["athlete"]
    kv_set("strava_tokens", tokens)


def exchange_code(code: str) -> None:
    r = httpx.post(TOKEN_URL, data={
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }, timeout=30)
    if r.status_code != 200:
        raise StravaError(f"token exchange failed ({r.status_code}): {r.text[:200]}")
    _store_tokens(r.json())


def status() -> dict:
    tokens = kv_get("strava_tokens")
    return {
        "configured": settings.strava_configured,
        "connected": bool(tokens or settings.strava_refresh_token),
        "athlete": (tokens or {}).get("athlete"),
    }


def disconnect() -> None:
    tokens = kv_get("strava_tokens")
    if tokens:
        try:
            httpx.post("https://www.strava.com/oauth/deauthorize",
                       data={"access_token": tokens["access_token"]}, timeout=15)
        except httpx.HTTPError:
            pass
    kv_delete("strava_tokens")


def access_token() -> str:
    if not settings.strava_configured:
        raise StravaError("STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET are not set")
    tokens = kv_get("strava_tokens") or {}
    if tokens and tokens["expires_at"] - 120 > time.time():
        return tokens["access_token"]
    refresh = tokens.get("refresh_token") or settings.strava_refresh_token
    if not refresh:
        raise StravaError("Strava is not connected - use Settings > Connect Strava")
    r = httpx.post(TOKEN_URL, data={
        "client_id": settings.strava_client_id,
        "client_secret": settings.strava_client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }, timeout=30)
    if r.status_code != 200:
        raise StravaError(f"token refresh failed ({r.status_code}): {r.text[:200]}")
    _store_tokens(r.json())
    return r.json()["access_token"]


def _set(activity_id: int, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE activities SET {sets} WHERE id = ?", [*fields.values(), activity_id])


DUPLICATE_RE = re.compile(r"duplicate of .*?activities/(\d+)|duplicate of activity (\d+)", re.I)


def upload(activity_id: int) -> None:
    """Upload the archived FIT file and poll until Strava has processed it.
    Runs in a background thread; progress is written to the activity row."""
    with connect() as conn:
        a = conn.execute("SELECT stored_path, file_hash FROM activities WHERE id = ?",
                         (activity_id,)).fetchone()
    try:
        token = access_token()
        headers = {"Authorization": f"Bearer {token}"}
        path = Path(a["stored_path"])
        with open(path, "rb") as f:
            r = httpx.post(f"{API}/uploads", headers=headers, timeout=60,
                           files={"file": (path.name, f, "application/octet-stream")},
                           data={"data_type": "fit", "external_id": f"soc-{a['file_hash'][:20]}"})
        if r.status_code == 429:
            raise StravaError("Strava rate limit reached - try again in 15 minutes")
        if r.status_code not in (200, 201):
            raise StravaError(f"upload rejected ({r.status_code}): {r.text[:200]}")
        up = r.json()
        _set(activity_id, strava_upload_id=up["id"])

        deadline = time.time() + POLL_TIMEOUT_S
        while True:
            if up.get("error"):
                m = DUPLICATE_RE.search(up["error"])
                if m:
                    _set(activity_id, strava_status="duplicate",
                         strava_activity_id=int(m.group(1) or m.group(2)), strava_error=None)
                    return
                raise StravaError(up["error"])
            if up.get("activity_id"):
                _set(activity_id, strava_status="done",
                     strava_activity_id=up["activity_id"], strava_error=None)
                return
            if time.time() > deadline:
                raise StravaError("Strava is still processing the upload - check again later")
            time.sleep(2)
            r = httpx.get(f"{API}/uploads/{up['id']}", headers=headers, timeout=30)
            if r.status_code != 200:
                raise StravaError(f"upload status check failed ({r.status_code})")
            up = r.json()
    except (StravaError, httpx.HTTPError, OSError) as e:
        log.warning("strava upload of activity %s failed: %s", activity_id, e)
        _set(activity_id, strava_status="error", strava_error=str(e))
