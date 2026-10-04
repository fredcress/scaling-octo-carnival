"""Runtime configuration, read once from environment variables (see .env.example)."""
import os
import secrets
from dataclasses import dataclass
from pathlib import Path


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    secret_key: str
    https_only: bool
    watch_dir: Path
    gadgetbridge_dir: Path
    data_dir: Path
    scan_interval: int
    max_hr: float
    resting_hr: float
    sex: str
    tz: str
    public_url: str
    strava_client_id: str
    strava_client_secret: str
    strava_refresh_token: str
    ollama_url: str = ""
    ollama_model: str = ""

    @property
    def coach_configured(self) -> bool:
        return bool(self.ollama_url and self.ollama_model)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "runs.db"

    @property
    def fit_store(self) -> Path:
        return self.data_dir / "fit"

    @property
    def strava_configured(self) -> bool:
        return bool(self.strava_client_id and self.strava_client_secret)


def _secret_key(data_dir: Path) -> str:
    """Use SECRET_KEY if given, otherwise generate one and persist it in the data dir
    so sessions survive container restarts."""
    key = os.environ.get("SECRET_KEY", "").strip()
    if key:
        return key
    path = data_dir / ".secret_key"
    if path.exists():
        return path.read_text().strip()
    key = secrets.token_urlsafe(48)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(key)
    path.chmod(0o600)
    return key


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        username=os.environ.get("APP_USERNAME", "admin"),
        password=os.environ.get("APP_PASSWORD", ""),
        secret_key=_secret_key(data_dir),
        https_only=os.environ.get("SESSION_HTTPS_ONLY", "false").lower() in ("1", "true", "yes"),
        watch_dir=Path(os.environ.get("WATCH_DIR", "/watch")),
        gadgetbridge_dir=Path(os.environ.get("GADGETBRIDGE_DIR", "/gadgetbridge")),
        data_dir=data_dir,
        scan_interval=max(10, int(_float("SCAN_INTERVAL", 60))),
        max_hr=_float("MAX_HR", 190),
        resting_hr=_float("RESTING_HR", 50),
        sex=os.environ.get("SEX", "male").lower(),
        tz=os.environ.get("TZ", "UTC"),
        public_url=os.environ.get("PUBLIC_URL", "").rstrip("/"),
        strava_client_id=os.environ.get("STRAVA_CLIENT_ID", "").strip(),
        strava_client_secret=os.environ.get("STRAVA_CLIENT_SECRET", "").strip(),
        strava_refresh_token=os.environ.get("STRAVA_REFRESH_TOKEN", "").strip(),
        ollama_url=os.environ.get("OLLAMA_URL", "").strip().rstrip("/"),
        ollama_model=os.environ.get("OLLAMA_MODEL", "").strip(),
    )


settings = load_settings()
