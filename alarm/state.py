"""
Runtime state shared across the daemon's scheduler loop, web server, and popup.

Holds the currently-ringing alarm and the snooze schedule. Thread-safe and
persisted to config/state.json so a daemon restart doesn't drop a pending
snooze or forget that an alarm is ringing.
"""

import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .config import get_repository
from .models import Alarm, RuntimeData


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

    def __init__(self, repository=None) -> None:
        self._lock = threading.RLock()
        self._repository = repository or get_repository()
        self._snoozes: Dict[str, datetime] = {}
        self._ringing: Optional[RingingInfo] = None
        self.load()

    # ---- persistence ----

    def load(self) -> None:
        with self._lock:
            self._sync_from_runtime(self._repository.snapshot().runtime)

    def _sync_from_runtime(self, runtime: RuntimeData) -> None:
        self._snoozes = {
            item["alarm_id"]: datetime.fromisoformat(item["due_at"])
            for item in runtime.snoozes
        }
        if runtime.active:
            fields = RingingInfo.__dataclass_fields__
            self._ringing = RingingInfo(
                **{name: runtime.active[name] for name in fields}
            )
        else:
            self._ringing = None

    def _mutate_runtime_locked(self, mutation: Callable[[RuntimeData], None]) -> None:
        def apply(document):
            mutation(document.runtime)
            return document.runtime

        runtime = self._repository.transaction(apply)
        self._sync_from_runtime(runtime)

    # ---- ringing ----

    def set_ringing(self, alarm: Alarm) -> None:
        with self._lock:
            ringing = RingingInfo(
                alarm_id=alarm.id,
                label=alarm.label,
                time=alarm.time,
                snoozable=alarm.snoozable,
                irritable=alarm.irritable,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._mutate_runtime_locked(
                lambda runtime: setattr(runtime, "active", asdict(ringing))
            )

    def clear_ringing(self) -> None:
        with self._lock:
            self._mutate_runtime_locked(lambda runtime: setattr(runtime, "active", None))

    def get_ringing(self) -> Optional[RingingInfo]:
        with self._lock:
            return RingingInfo(**asdict(self._ringing)) if self._ringing else None

    # ---- snooze ----

    def set_snooze(self, alarm_id: str, when: datetime) -> None:
        with self._lock:
            def set_value(runtime: RuntimeData) -> None:
                runtime.snoozes = [
                    item for item in runtime.snoozes if item["alarm_id"] != alarm_id
                ]
                runtime.snoozes.append({"alarm_id": alarm_id, "due_at": when.isoformat()})

            self._mutate_runtime_locked(set_value)

    def cancel_snooze(self, alarm_id: str) -> None:
        with self._lock:
            self._mutate_runtime_locked(
                lambda runtime: setattr(
                    runtime,
                    "snoozes",
                    [item for item in runtime.snoozes if item["alarm_id"] != alarm_id],
                )
            )

    def pop_due_snoozes(self, now: Optional[datetime] = None) -> List[str]:
        """Return alarm IDs whose snooze is due, removing them from the schedule."""
        now = now or datetime.now()
        with self._lock:
            due: List[str] = []

            def pop(runtime: RuntimeData) -> None:
                nonlocal due
                due = [
                    item["alarm_id"]
                    for item in runtime.snoozes
                    if now >= datetime.fromisoformat(item["due_at"])
                ]
                runtime.snoozes = [
                    item for item in runtime.snoozes if item["alarm_id"] not in due
                ]

            self._mutate_runtime_locked(pop)
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
