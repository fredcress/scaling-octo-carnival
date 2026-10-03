import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

# settings are read at import time, so the environment must be ready first
_tmp = Path(tempfile.mkdtemp(prefix="soc-test-"))
os.environ.update({
    "DATA_DIR": str(_tmp / "data"),
    "WATCH_DIR": str(_tmp / "watch"),
    "APP_USERNAME": "runner",
    "APP_PASSWORD": "secret",
    "MAX_HR": "190",
    "RESTING_HR": "50",
    "TZ": "Europe/Paris",
    "STRAVA_CLIENT_ID": "",
    "STRAVA_CLIENT_SECRET": "",
})
(_tmp / "watch").mkdir(parents=True)

from fit_tool.profile.profile_type import Sport, SubSport  # noqa: E402
from make_demo_data import write_activity  # noqa: E402

from app.db import init_db  # noqa: E402


@pytest.fixture(scope="session")
def watch_dir() -> Path:
    """A watch folder with two runs, a walk and a corrupt file, all older than the
    scanner's settle time."""
    d = _tmp / "watch"
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3, hours=2)
    write_activity(d / "run1.fit", start, Sport.RUNNING, SubSport.GENERIC, 5.4, 330, 0.7, seed=1)
    (d / "sub").mkdir()
    write_activity(d / "sub" / "run2.fit", start + timedelta(days=1), Sport.RUNNING, SubSport.TRAIL, 3.2, 300, 0.8,
                   intervals=True, seed=2)
    write_activity(d / "walk.fit", start + timedelta(days=2), Sport.WALKING, SubSport.GENERIC,
                   2.0, 660, 0.35, seed=3)
    (d / "broken.fit").write_bytes(b"\x0e\x10garbage" * 10)
    old = (datetime.now() - timedelta(minutes=5)).timestamp()
    for p in d.rglob("*.fit"):
        os.utime(p, (old, old))
    return d


@pytest.fixture(scope="session")
def imported(watch_dir):
    from app import watcher
    init_db()
    return watcher.scan_once()
