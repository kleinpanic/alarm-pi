"""
Scheduling logic for determining when alarms should fire.

Handles time matching, day-of-week checks, skip dates, and snooze scheduling.
Snooze state lives in a shared RuntimeState when one is provided (the daemon),
so it is shared with the web server and survives restarts. Without a state
(e.g. the CLI computing `next`), snooze is unused.
"""

from datetime import (
    datetime,
    time,
    timedelta,
    timezone as datetime_timezone,
    tzinfo,
)
from typing import Iterable, List, Optional, Set
import logging

from .models import (
    Alarm,
    AlarmSnapshot,
    Occurrence,
    SchedulerDiagnostic,
    SchedulerEvaluation,
)
from .state import RuntimeState

logger = logging.getLogger(__name__)

CATCH_UP_MINUTES = 15
__all__ = ["AlarmScheduler", "CATCH_UP_MINUTES"]


class AlarmScheduler:
    """Pure interval scheduling plus the legacy exact-minute compatibility API."""

    def __init__(self, state: Optional[RuntimeState] = None):
        self.state = state
        # Track fired alarms this minute: set of (alarm_id, "HH:MM", "YYYY-MM-DD")
        self._fired_this_minute: Set[tuple] = set()
        self._last_check_minute: Optional[str] = None

    def evaluate_interval(
        self,
        alarms: Iterable[Alarm],
        checkpoint: Optional[datetime],
        now: datetime,
        timezone: tzinfo,
        *,
        accepted_occurrence_ids: Iterable[str] = (),
        snoozes: Iterable[Occurrence] = (),
    ) -> SchedulerEvaluation:
        """Evaluate scheduled and snoozed candidates over ``(checkpoint, now]``.

        This method is deliberately free of persistence and active-alarm side
        effects.  A missing checkpoint means first-run initialization, not
        recovery, so it never scans backward from ``now``.
        """
        self._require_aware(now, "now")
        if checkpoint is None:
            return SchedulerEvaluation((), (), now)
        self._require_aware(checkpoint, "checkpoint")
        alarms = tuple(alarms)
        snoozes = tuple(snoozes)

        if now.astimezone(datetime_timezone.utc) < checkpoint.astimezone(datetime_timezone.utc):
            return SchedulerEvaluation(
                (),
                (SchedulerDiagnostic(code="clock_backward", observed_at=now),),
                checkpoint,
            )

        accepted = set(accepted_occurrence_ids)
        occurrences: list[Occurrence] = []
        diagnostics: list[SchedulerDiagnostic] = []
        checkpoint_utc = checkpoint.astimezone(datetime_timezone.utc)
        now_utc = now.astimezone(datetime_timezone.utc)
        catch_up_start = now_utc - timedelta(minutes=CATCH_UP_MINUTES)
        start_date = checkpoint.astimezone(timezone).date()
        end_date = now.astimezone(timezone).date()
        day_count = (end_date - start_date).days

        for offset in range(day_count + 1):
            local_date = start_date + timedelta(days=offset)
            for alarm in alarms:
                if not alarm.enabled or local_date.weekday() not in alarm.days_of_week:
                    continue
                date_text = local_date.isoformat()
                if date_text in alarm.skip_dates:
                    continue
                try:
                    hour, minute = map(int, alarm.time.split(":"))
                    local_slot = datetime.combine(local_date, time(hour, minute))
                except (TypeError, ValueError):
                    continue
                occurrence_id = f"scheduled:{alarm.id}:{date_text}:{alarm.time}"
                if occurrence_id in accepted:
                    continue
                due_at = self._resolve_local_slot(local_slot, timezone)
                if due_at is None:
                    if self._local_slot_in_wall_interval(local_slot, checkpoint, now, timezone):
                        diagnostics.append(
                            SchedulerDiagnostic(
                                code="skipped_nonexistent",
                                observed_at=now,
                                occurrence_id=occurrence_id,
                                alarm_id=alarm.id,
                                scheduled_for=local_slot.isoformat(timespec="minutes"),
                            )
                        )
                    continue
                due_utc = due_at.astimezone(datetime_timezone.utc)
                if not checkpoint_utc < due_utc <= now_utc:
                    continue
                if due_utc < catch_up_start:
                    diagnostics.append(
                        SchedulerDiagnostic(
                            code="missed_stale",
                            observed_at=now,
                            occurrence_id=occurrence_id,
                            alarm_id=alarm.id,
                            due_at=due_at,
                            scheduled_for=local_slot.isoformat(timespec="minutes"),
                        )
                    )
                    continue
                occurrences.append(
                    Occurrence(
                        occurrence_id=occurrence_id,
                        alarm_id=alarm.id,
                        kind="scheduled",
                        due_at=due_at,
                        accepted_at=None,
                        alarm=AlarmSnapshot.from_alarm(alarm),
                    )
                )

        for snooze in snoozes:
            self._require_aware(snooze.due_at, "snooze due_at")
            due_utc = snooze.due_at.astimezone(datetime_timezone.utc)
            if snooze.occurrence_id in accepted or not checkpoint_utc < due_utc <= now_utc:
                continue
            if due_utc < catch_up_start:
                diagnostics.append(
                    SchedulerDiagnostic(
                        code="missed_stale",
                        observed_at=now,
                        occurrence_id=snooze.occurrence_id,
                        alarm_id=snooze.alarm_id,
                        due_at=snooze.due_at,
                    )
                )
                continue
            occurrences.append(snooze)

        occurrences.sort(
            key=lambda item: (
                item.due_at.astimezone(datetime_timezone.utc),
                0 if item.kind == "scheduled" else 1,
                item.alarm_id,
            )
        )
        diagnostics.sort(
            key=lambda item: (
                item.due_at.astimezone(datetime_timezone.utc)
                if item.due_at is not None
                else now_utc,
                item.code,
                item.alarm_id or "",
                item.occurrence_id or "",
            )
        )
        return SchedulerEvaluation(tuple(occurrences), tuple(diagnostics), now)

    @staticmethod
    def _resolve_local_slot(local_slot: datetime, timezone: tzinfo) -> Optional[datetime]:
        """Resolve a wall-clock slot, choosing fold zero and rejecting DST gaps."""
        resolved: list[datetime] = []
        for fold in (0, 1):
            candidate = local_slot.replace(tzinfo=timezone, fold=fold)
            round_trip = candidate.astimezone(datetime_timezone.utc).astimezone(timezone)
            if round_trip.replace(tzinfo=None) == local_slot:
                resolved.append(candidate)
        if not resolved:
            return None
        return min(resolved, key=lambda item: item.astimezone(datetime_timezone.utc))

    @staticmethod
    def _local_slot_in_wall_interval(
        local_slot: datetime,
        checkpoint: datetime,
        now: datetime,
        timezone: tzinfo,
    ) -> bool:
        start = checkpoint.astimezone(timezone).replace(tzinfo=None)
        end = now.astimezone(timezone).replace(tzinfo=None)
        return start < local_slot <= end

    @staticmethod
    def _require_aware(value: datetime, name: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")

    def check_alarms(self, alarms: List[Alarm], now: Optional[datetime] = None) -> List[Alarm]:
        """Compatibility wrapper for callers not yet migrated to interval admission."""
        if now is None:
            now = datetime.now()

        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")
        current_weekday = now.weekday()  # 0=Monday, 6=Sunday

        if self._last_check_minute != current_time:
            self._fired_this_minute.clear()
            self._last_check_minute = current_time

        to_fire = []
        for alarm in alarms:
            if not alarm.enabled:
                continue
            fire_key = (alarm.id, current_time, current_date)
            if fire_key in self._fired_this_minute:
                continue
            if self._should_fire(alarm, current_time, current_date, current_weekday):
                to_fire.append(alarm)
                self._fired_this_minute.add(fire_key)
                logger.info(f"Alarm '{alarm.label}' (id={alarm.id}) triggered")

        # Due snoozes
        if self.state is not None:
            by_id = {a.id: a for a in alarms}
            for alarm_id in self.state.pop_due_snoozes(now):
                alarm = by_id.get(alarm_id)
                if alarm and alarm not in to_fire:
                    to_fire.append(alarm)
                    logger.info(f"Snoozed alarm '{alarm.label}' (id={alarm.id}) triggered")

        return to_fire

    def _should_fire(self, alarm: Alarm, current_time: str,
                     current_date: str, current_weekday: int) -> bool:
        """Check if a specific alarm should fire now."""
        if alarm.time != current_time:
            return False
        if current_weekday not in alarm.days_of_week:
            return False
        if current_date in alarm.skip_dates:
            logger.debug(f"Alarm {alarm.id} skipped due to skip_date {current_date}")
            return False
        return True

    def schedule_snooze(self, alarm: Alarm, snooze_minutes: int,
                        now: Optional[datetime] = None) -> datetime:
        """Schedule an alarm to re-fire after snooze_minutes."""
        snooze_time = (now or datetime.now()) + timedelta(minutes=snooze_minutes)
        if self.state is not None:
            self.state.set_snooze(alarm.id, snooze_time)
        logger.info(f"Alarm '{alarm.label}' snoozed until {snooze_time.strftime('%H:%M:%S')}")
        return snooze_time

    def cancel_snooze(self, alarm_id: str) -> None:
        """Cancel a pending snooze for an alarm."""
        if self.state is not None:
            self.state.cancel_snooze(alarm_id)

    def get_next_alarms(self, alarms: List[Alarm], count: int = 5,
                        now: Optional[datetime] = None) -> List[tuple]:
        """Return the next N upcoming (alarm, datetime) triggers, sorted."""
        if now is None:
            now = datetime.now()

        upcoming = []
        for days_ahead in range(8):  # look ahead 7 days
            check_date = now + timedelta(days=days_ahead)
            check_weekday = check_date.weekday()
            date_str = check_date.strftime("%Y-%m-%d")

            for alarm in alarms:
                if not alarm.enabled or check_weekday not in alarm.days_of_week:
                    continue
                if date_str in alarm.skip_dates:
                    continue
                try:
                    hour, minute = map(int, alarm.time.split(":"))
                    fire_dt = check_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if fire_dt <= now:
                        continue
                    upcoming.append((alarm, fire_dt))
                except ValueError:
                    continue

        upcoming.sort(key=lambda x: x[1])
        return upcoming[:count]
