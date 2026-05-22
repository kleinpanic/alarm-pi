"""
Runtime state shared across the daemon's scheduler loop, web server, and popup.

Holds the currently-ringing alarm and the snooze schedule. Thread-safe and
persisted to config/state.json so a daemon restart doesn't drop a pending
snooze or forget that an alarm is ringing.
"""

import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional

from .config import STATE_FILE, _atomic_write_json
from .models import Alarm

logger = logging.getLogger(__name__)


@dataclass
class RingingInfo:
    """Snapshot of the alarm currently ringing."""
    alarm_id: str
    label: str
    time: str
    snoozable: bool
    irritable: bool
    started_at: str  # ISO timestamp


class RuntimeState:
    """Thread-safe runtime state with JSON persistence."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snoozes: Dict[str, datetime] = {}
        self._ringing: Optional[RingingInfo] = None
        self.load()

    # ---- persistence ----

    def load(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read state.json ({e}); starting clean")
            return
        with self._lock:
            self._snoozes = {
                aid: datetime.fromisoformat(ts)
                for aid, ts in (data.get("snoozes") or {}).items()
            }
            r = data.get("ringing")
            self._ringing = RingingInfo(**r) if r else None

    def _persist_locked(self) -> None:
        data = {
            "snoozes": {aid: dt.isoformat() for aid, dt in self._snoozes.items()},
            "ringing": asdict(self._ringing) if self._ringing else None,
        }
        try:
            _atomic_write_json(STATE_FILE, data)
        except OSError as e:
            logger.error(f"Failed to persist state.json: {e}")

    # ---- ringing ----

    def set_ringing(self, alarm: Alarm) -> None:
        with self._lock:
            self._ringing = RingingInfo(
                alarm_id=alarm.id,
                label=alarm.label,
                time=alarm.time,
                snoozable=alarm.snoozable,
                irritable=alarm.irritable,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._persist_locked()

    def clear_ringing(self) -> None:
        with self._lock:
            self._ringing = None
            self._persist_locked()

    def get_ringing(self) -> Optional[RingingInfo]:
        with self._lock:
            return self._ringing

    # ---- snooze ----

    def set_snooze(self, alarm_id: str, when: datetime) -> None:
        with self._lock:
            self._snoozes[alarm_id] = when
            self._persist_locked()

    def cancel_snooze(self, alarm_id: str) -> None:
        with self._lock:
            if self._snoozes.pop(alarm_id, None) is not None:
                self._persist_locked()

    def pop_due_snoozes(self, now: Optional[datetime] = None) -> List[str]:
        """Return alarm IDs whose snooze is due, removing them from the schedule."""
        now = now or datetime.now()
        with self._lock:
            due = [aid for aid, when in self._snoozes.items() if now >= when]
            if due:
                for aid in due:
                    del self._snoozes[aid]
                self._persist_locked()
            return due

    def snoozes(self) -> Dict[str, datetime]:
        with self._lock:
            return dict(self._snoozes)

    # ---- status snapshot ----

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "ringing": asdict(self._ringing) if self._ringing else None,
                "snoozes": {aid: dt.isoformat() for aid, dt in self._snoozes.items()},
            }
