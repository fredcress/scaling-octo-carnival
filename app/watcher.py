"""Polls the Syncthing folder for new .fit files.

Polling (not inotify) because inotify is unreliable on NFS/SMB and through some
container bind mounts; scanning a few hundred small files per minute is cheap."""
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import coach, health
from .config import settings
from .db import connect, kv_set
from .fitimport import import_file

log = logging.getLogger(__name__)

_lock = threading.Lock()
_wake = threading.Event()
SETTLE_S = 15  # skip files modified this recently: Syncthing may still be writing


def _candidates(root: Path):
    for p in root.rglob("*"):
        if (p.is_file() and p.suffix.lower() == ".fit"
                and not p.name.startswith((".syncthing", "~syncthing"))
                and ".stversions" not in p.parts):
            yield p


def scan_once() -> dict:
    """Import every new or changed .fit file. Safe to call concurrently (serialised)."""
    counts = {"imported": 0, "duplicate": 0, "skipped": 0, "error": 0}
    if not settings.watch_dir.is_dir():
        log.warning("watch dir %s does not exist", settings.watch_dir)
        return counts
    with _lock:
        with connect() as conn:
            seen = {r["path"]: (r["size"], r["mtime"]) for r in
                    conn.execute("SELECT path, size, mtime FROM seen_files")}
        now = time.time()
        for path in sorted(_candidates(settings.watch_dir)):
            try:
                st = path.stat()
            except OSError:
                continue
            key = str(path)
            if seen.get(key) == (st.st_size, st.st_mtime) or now - st.st_mtime < SETTLE_S:
                continue
            status, message, activity_id = import_file(path)
            counts[status] += 1
            log.info("%s: %s (%s)", path.name, status, message)
            with connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO seen_files(path, size, mtime, file_hash, status, message)"
                    " VALUES(?, ?, ?, (SELECT file_hash FROM activities WHERE id = ?), ?, ?)",
                    (key, st.st_size, st.st_mtime, activity_id, status, message))
        kv_set("last_scan", {"at": datetime.now(timezone.utc).isoformat(), **counts})
    return counts


def trigger() -> None:
    _wake.set()


def run_forever() -> None:
    while True:
        try:
            scan_once()
        except Exception:
            log.exception("scan failed")
        try:
            health.import_once()
        except Exception:
            log.exception("health import failed")
        try:
            coach.generate_in_background()  # no-op unless the facts changed
        except Exception:
            log.exception("coach failed")
        _wake.wait(settings.scan_interval)
        _wake.clear()


def start() -> None:
    threading.Thread(target=run_forever, name="fit-watcher", daemon=True).start()
