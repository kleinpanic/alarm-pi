"""
Configuration loading, saving, and management.

Handles JSON file I/O with automatic directory/file creation, atomic writes,
and a cross-process file lock so the CLI and the daemon's web server don't
corrupt each other on concurrent read-modify-write.
"""

import json
import os
import fcntl
import tempfile
import contextlib
from pathlib import Path
from typing import List, Optional, Tuple
import logging

from .models import Alarm, Settings
from .repository import OperationalRepository

logger = logging.getLogger(__name__)

# Project root: defaults to the repo root (parent of this package),
# overridable via the ALARM_PROJECT_ROOT environment variable.
PROJECT_ROOT = Path(os.environ.get("ALARM_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent)))
CONFIG_DIR = PROJECT_ROOT / "config"
ALARMS_FILE = CONFIG_DIR / "alarms.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOCK_FILE = CONFIG_DIR / ".alarm.lock"
OPERATIONAL_FILE = CONFIG_DIR / "operational.json"
OPERATIONAL_LOCK_FILE = CONFIG_DIR / "operational.lock"
BACKUP_DIR = CONFIG_DIR / "backups"
QUARANTINE_DIR = CONFIG_DIR / "quarantine"

_repository: Optional[OperationalRepository] = None


def get_repository() -> OperationalRepository:
    """Return the process-local repository using the compatibility config root."""
    global _repository
    if _repository is None:
        _repository = OperationalRepository(CONFIG_DIR)
    return _repository


def ensure_config_dir() -> None:
    """Create config directory if it doesn't exist."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def config_lock():
    """Exclusive cross-process lock around read-modify-write sequences."""
    ensure_config_dir()
    with open(LOCK_FILE, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON to a temp file in the same dir, then atomically replace."""
    ensure_config_dir()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_settings() -> Settings:
    """Load a detached settings snapshot from the operational repository."""
    return get_repository().snapshot().settings


def save_settings(settings: Settings) -> None:
    """Validate and save settings through the aggregate transaction."""
    get_repository().set_settings(settings)


def load_alarms() -> List[Alarm]:
    """Load detached alarms from the operational repository."""
    return get_repository().snapshot().alarms


def save_alarms(alarms: List[Alarm]) -> None:
    """Replace alarms through one aggregate transaction."""
    get_repository().replace_alarms(alarms)


def get_alarm_by_id(alarm_id: str) -> Optional[Alarm]:
    """Find alarm by ID."""
    for alarm in load_alarms():
        if alarm.id == alarm_id:
            return alarm
    return None


def update_alarm(alarm: Alarm) -> bool:
    """Update existing alarm. Returns True if found and updated."""
    return get_repository().update_alarm(alarm)


def delete_alarm(alarm_id: str) -> bool:
    """Delete alarm by ID. Returns True if found and deleted."""
    return get_repository().delete_alarm(alarm_id)


def add_alarm(alarm: Alarm) -> Alarm:
    """Create an alarm with its repository-allocated ID."""
    return get_repository().create_alarm(alarm)


def generate_alarm_id() -> str:
    """Generate a unique alarm ID based on existing IDs."""
    existing_ids = {a.id for a in load_alarms()}
    for i in range(1, 10000):
        if str(i) not in existing_ids:
            return str(i)
    import time
    return str(int(time.time()))


def get_config_mtime() -> Tuple[float, float]:
    """Return aggregate mtime in the legacy two-value compatibility shape."""
    mtime = OPERATIONAL_FILE.stat().st_mtime if OPERATIONAL_FILE.exists() else 0.0
    return (mtime, mtime)
