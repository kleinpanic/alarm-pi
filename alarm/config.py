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

logger = logging.getLogger(__name__)

# Project root: defaults to the repo root (parent of this package),
# overridable via the ALARM_PROJECT_ROOT environment variable.
PROJECT_ROOT = Path(os.environ.get("ALARM_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent)))
CONFIG_DIR = PROJECT_ROOT / "config"
ALARMS_FILE = CONFIG_DIR / "alarms.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
STATE_FILE = CONFIG_DIR / "state.json"
LOCK_FILE = CONFIG_DIR / ".alarm.lock"


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
    """Load settings from JSON file, creating defaults if missing."""
    ensure_config_dir()

    if not SETTINGS_FILE.exists():
        logger.info(f"Settings file not found, creating defaults at {SETTINGS_FILE}")
        settings = Settings()
        save_settings(settings)
        return settings

    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        return Settings.from_dict(data)
    except json.JSONDecodeError as e:
        logger.error(f"Malformed settings JSON: {e}; using defaults")
        return Settings()
    except Exception as e:
        logger.error(f"Error loading settings: {e}")
        return Settings()


def save_settings(settings: Settings) -> None:
    """Save settings to JSON file (atomic)."""
    _atomic_write_json(SETTINGS_FILE, settings.to_dict())
    logger.debug(f"Settings saved to {SETTINGS_FILE}")


def load_alarms() -> List[Alarm]:
    """Load alarms from JSON file. Returns empty list if missing/malformed."""
    ensure_config_dir()

    if not ALARMS_FILE.exists():
        logger.info(f"Alarms file not found at {ALARMS_FILE}, starting empty")
        save_alarms([])
        return []

    try:
        with open(ALARMS_FILE, "r") as f:
            data = json.load(f)

        if not isinstance(data, list):
            logger.error("Alarms JSON should be a list")
            return []

        return [Alarm.from_dict(item) for item in data]
    except json.JSONDecodeError as e:
        logger.error(f"Malformed alarms JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"Error loading alarms: {e}")
        return []


def save_alarms(alarms: List[Alarm]) -> None:
    """Save alarms to JSON file (atomic)."""
    _atomic_write_json(ALARMS_FILE, [a.to_dict() for a in alarms])
    logger.debug(f"Saved {len(alarms)} alarms to {ALARMS_FILE}")


def get_alarm_by_id(alarm_id: str) -> Optional[Alarm]:
    """Find alarm by ID."""
    for alarm in load_alarms():
        if alarm.id == alarm_id:
            return alarm
    return None


def update_alarm(alarm: Alarm) -> bool:
    """Update existing alarm. Returns True if found and updated."""
    with config_lock():
        alarms = load_alarms()
        for i, a in enumerate(alarms):
            if a.id == alarm.id:
                alarms[i] = alarm
                save_alarms(alarms)
                return True
    return False


def delete_alarm(alarm_id: str) -> bool:
    """Delete alarm by ID. Returns True if found and deleted."""
    with config_lock():
        alarms = load_alarms()
        kept = [a for a in alarms if a.id != alarm_id]
        if len(kept) < len(alarms):
            save_alarms(kept)
            return True
    return False


def add_alarm(alarm: Alarm) -> None:
    """Add new alarm to config."""
    with config_lock():
        alarms = load_alarms()
        alarms.append(alarm)
        save_alarms(alarms)


def generate_alarm_id() -> str:
    """Generate a unique alarm ID based on existing IDs."""
    existing_ids = {a.id for a in load_alarms()}
    for i in range(1, 10000):
        if str(i) not in existing_ids:
            return str(i)
    import time
    return str(int(time.time()))


def get_config_mtime() -> Tuple[float, float]:
    """Return (alarms_mtime, settings_mtime); 0.0 for missing files."""
    alarms_mtime = ALARMS_FILE.stat().st_mtime if ALARMS_FILE.exists() else 0.0
    settings_mtime = SETTINGS_FILE.stat().st_mtime if SETTINGS_FILE.exists() else 0.0
    return (alarms_mtime, settings_mtime)
