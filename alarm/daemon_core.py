"""
Core daemon: scheduler loop + in-process web server + non-blocking alarm firing.

The scheduler loop never blocks on UI. When an alarm fires, the daemon records
it in RuntimeState, starts audio, and (if a display exists) spawns the popup as
a child process. Dismiss/snooze/test are control methods invoked by the web
server, the CLI (over HTTP), and the popup (over HTTP) — one unified path.
"""

import os
import sys
import time
import signal
import logging
import threading
import subprocess
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone
from typing import Iterable, Optional

from .config import ALARMS_FILE, get_config_mtime
from .models import Alarm, AlarmSnapshot, Occurrence, SchedulerDiagnostic
from .repository import OperationalRepository
from .scheduler import AlarmScheduler, CATCH_UP_MINUTES
from .audio import AudioPlayer
from .state import RuntimeState

logger = logging.getLogger(__name__)

SNOOZE_MIN_MINUTES = 1
SNOOZE_MAX_MINUTES = 60


class AlarmDaemon:
    """Runs the scheduling loop and hosts the web server."""

    def __init__(
        self,
        *,
        repository: Optional[OperationalRepository] = None,
        audio=None,
        clock=None,
        timezone=None,
    ):
        from .config import get_repository

        self.repository = repository or get_repository()
        document = self.repository.snapshot()
        self.settings = document.settings
        self.alarms = document.alarms
        self.state = RuntimeState(repository=self.repository)
        self.scheduler = AlarmScheduler(state=self.state)
        self.audio = audio or AudioPlayer(self.settings)
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.timezone = timezone or self._clock().tzinfo or datetime_timezone.utc

        self._running = False
        self._config_mtime = get_config_mtime()
        self._popup_proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._web_thread: Optional[threading.Thread] = None
        self._recovered = False

    # ---- lifecycle ----

    def run(self) -> None:
        logger.info("Alarm daemon starting")
        logger.info(f"Config directory: {ALARMS_FILE.parent}")
        logger.info(f"Loaded {len(self.alarms)} alarm(s)")
        if self.settings.web_enabled:
            self._start_web()
        else:
            logger.info("Web UI disabled (web_enabled=false)")
        self._running = True
        self.recover()

        while self._running:
            try:
                self._check_config_reload()
                self.poll(self._clock())
                time.sleep(self.settings.check_interval_seconds)
            except KeyboardInterrupt:
                logger.info("Received interrupt, shutting down")
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(1)

        self.shutdown()

    def shutdown(self) -> None:
        logger.info("Daemon shutting down")
        self._running = False
        self.audio.stop()
        self._kill_popup()

    def _check_config_reload(self) -> None:
        current = get_config_mtime()
        if current != self._config_mtime:
            document = self.repository.snapshot()
            new_settings = document.settings
            self._config_mtime = current

            # Runtime checkpoints, queue changes, and diagnostics share the
            # operational document with configuration. Those writes update the
            # file mtime on every scheduler pass but are not configuration
            # changes and must not trigger a full daemon reload.
            if new_settings == self.settings and document.alarms == self.alarms:
                return

            logger.info("Config change detected, reloading")
            self.alarms = document.alarms
            # Keep the existing AudioPlayer if mid-ring; just refresh settings.
            self.settings = new_settings
            self.audio.settings = new_settings
            logger.info(f"Reloaded {len(self.alarms)} alarm(s)")
            # React to a runtime web_enabled change.
            web_alive = self._web_thread is not None and self._web_thread.is_alive()
            if self.settings.web_enabled and not web_alive:
                logger.info("web_enabled turned on; starting web server")
                self._start_web()
            elif not self.settings.web_enabled and web_alive:
                logger.warning("web_enabled turned off; web server runs until daemon restart")

    def _start_web(self) -> None:
        """Launch the web server in an isolated daemon thread.

        Gated by ``settings.web_enabled`` (default True), evaluated at runtime so
        a config change can bring it up. Idempotent: a no-op if already running.
        Any web failure is contained in the thread and never affects the
        alarm scheduler loop.
        """
        if not self.settings.web_enabled:
            return
        if self._web_thread is not None and self._web_thread.is_alive():
            return
        self._web_thread = threading.Thread(
            target=self._web_thread_main, daemon=True, name="alarm-web"
        )
        self._web_thread.start()

    def _web_thread_main(self) -> None:
        """Thread entrypoint that fully isolates web failures from the daemon."""
        try:
            from .web.server import run_server
        except Exception as e:
            logger.error(f"Web server unavailable ({e}); running headless")
            return
        try:
            logger.info(f"Web UI on http://{self.settings.web_host}:{self.settings.web_port}")
            run_server(self)
        except Exception as e:
            logger.error(f"Web server stopped ({e}); alarm core unaffected", exc_info=True)
        logger.info(f"Web UI on http://{self.settings.web_host}:{self.settings.web_port}")

    # ---- ringing / control ----

    @staticmethod
    def _alarm_from_record(record: dict) -> Alarm:
        return Alarm.from_payload(record["alarm"])

    @staticmethod
    def _occurrence_record(occurrence: Occurrence, accepted_at: datetime) -> dict:
        snapshot = occurrence.alarm
        return {
            "occurrence_id": occurrence.occurrence_id,
            "alarm_id": occurrence.alarm_id,
            "label": snapshot.label,
            "time": snapshot.time,
            "snoozable": snapshot.snoozable,
            "irritable": snapshot.irritable,
            "started_at": accepted_at.isoformat(),
            "kind": occurrence.kind,
            "due_at": occurrence.due_at.isoformat(),
            "accepted_at": accepted_at.isoformat(),
            "alarm": {
                "id": snapshot.id,
                "label": snapshot.label,
                "time": snapshot.time,
                "days_of_week": list(snapshot.days_of_week),
                "enabled": snapshot.enabled,
                "snoozable": snapshot.snoozable,
                "irritable": snapshot.irritable,
                "sound_path": snapshot.sound_path,
                "base_volume": snapshot.base_volume,
                "irritable_duration_minutes": snapshot.irritable_duration_minutes,
                "irritable_volume_step": snapshot.irritable_volume_step,
                "skip_dates": list(snapshot.skip_dates),
            },
        }

    @staticmethod
    def _diagnostic_record(diagnostic: SchedulerDiagnostic) -> dict:
        return {
            "code": diagnostic.code,
            "observed_at": diagnostic.observed_at.isoformat(),
            "occurrence_id": diagnostic.occurrence_id,
            "alarm_id": diagnostic.alarm_id,
            "due_at": diagnostic.due_at.isoformat() if diagnostic.due_at else None,
            "scheduled_for": diagnostic.scheduled_for,
        }

    @staticmethod
    def _queue_key(record: dict) -> tuple:
        due = datetime.fromisoformat(record["due_at"]).astimezone(datetime_timezone.utc)
        return (due, 0 if record["kind"] == "scheduled" else 1, record["alarm_id"])

    @staticmethod
    def _eligibility_code(document, record: dict) -> Optional[str]:
        alarm = next((item for item in document.alarms if item.id == record["alarm_id"]), None)
        if alarm is None:
            return "snooze_source_missing" if record["kind"] == "snooze" else "source_missing"
        if not alarm.enabled:
            return "snooze_source_disabled" if record["kind"] == "snooze" else "source_disabled"
        if record["kind"] == "snooze" and not alarm.snoozable:
            return "snooze_source_not_snoozable"
        return None

    def poll(self, now: datetime) -> int:
        """Evaluate and durably admit one aware scheduler interval."""
        snapshot = self.repository.snapshot()
        checkpoint = (
            datetime.fromisoformat(snapshot.runtime.scheduler_checkpoint)
            if snapshot.runtime.scheduler_checkpoint
            else None
        )
        snoozes = []
        for record in snapshot.runtime.snoozes:
            if "occurrence_id" not in record or "alarm" not in record:
                continue
            snoozes.append(
                Occurrence(
                    occurrence_id=record["occurrence_id"],
                    alarm_id=record["alarm_id"],
                    kind="snooze",
                    due_at=datetime.fromisoformat(record["due_at"]),
                    accepted_at=None,
                    alarm=AlarmSnapshot.from_alarm(self._alarm_from_record(record)),
                )
            )
        result = self.scheduler.evaluate_interval(
            snapshot.alarms,
            checkpoint,
            now,
            self.timezone,
            accepted_occurrence_ids=snapshot.runtime.accepted_occurrences,
            snoozes=snoozes,
        )
        return self.admit_occurrences(result.occurrences, result.diagnostics, result.checkpoint)

    def admit_occurrences(
        self,
        occurrences: Iterable[Occurrence],
        diagnostics: Iterable[SchedulerDiagnostic],
        checkpoint: datetime,
    ) -> int:
        """Persist admission and checkpoint atomically, then start side effects."""
        occurrences = sorted(
            tuple(occurrences),
            key=lambda item: self._queue_key(self._occurrence_record(item, checkpoint)),
        )
        diagnostics = tuple(diagnostics)
        accepted_count = 0
        active_before = None
        active_after = None

        def admit(document):
            nonlocal accepted_count, active_before, active_after
            runtime = document.runtime
            active_before = runtime.active
            runtime.diagnostics.extend(self._diagnostic_record(item) for item in diagnostics)
            runtime.scheduler_checkpoint = checkpoint.isoformat()
            accepted = set(runtime.accepted_occurrences)
            for occurrence in occurrences:
                if occurrence.occurrence_id in accepted:
                    continue
                record = self._occurrence_record(occurrence, checkpoint)
                code = (
                    self._eligibility_code(document, record)
                    if occurrence.kind == "snooze"
                    else None
                )
                if occurrence.kind == "snooze":
                    runtime.snoozes = [
                        item
                        for item in runtime.snoozes
                        if item.get("occurrence_id") != occurrence.occurrence_id
                    ]
                if code:
                    runtime.accepted_occurrences.append(occurrence.occurrence_id)
                    accepted.add(occurrence.occurrence_id)
                    runtime.diagnostics.append(
                        {
                            "code": code,
                            "observed_at": checkpoint.isoformat(),
                            "occurrence_id": occurrence.occurrence_id,
                            "alarm_id": occurrence.alarm_id,
                            "due_at": occurrence.due_at.isoformat(),
                        }
                    )
                    continue
                runtime.accepted_occurrences.append(occurrence.occurrence_id)
                accepted.add(occurrence.occurrence_id)
                if runtime.active is None:
                    runtime.active = record
                else:
                    runtime.queue.append(record)
                    runtime.queue.sort(key=self._queue_key)
                accepted_count += 1
            active_after = runtime.active

        with self._lock:
            self.repository.transaction(admit)
            self.state.load()
            if active_before is None and active_after is not None:
                self._start_active_effects(active_after)
        return accepted_count

    def _start_active_effects(self, record: dict) -> None:
        alarm = self._alarm_from_record(record)
        logger.info("Firing alarm: '%s' (id=%s)", alarm.label, alarm.id)
        if not self.audio.play(alarm):
            logger.error("Failed to start audio playback")
        self._spawn_popup(alarm)

    def _promote_valid(self, document, now: datetime) -> None:
        while document.runtime.queue:
            record = document.runtime.queue.pop(0)
            code = self._eligibility_code(document, record)
            if code is None:
                document.runtime.active = record
                return
            document.runtime.diagnostics.append(
                {
                    "code": code,
                    "observed_at": now.isoformat(),
                    "occurrence_id": record.get("occurrence_id"),
                    "alarm_id": record.get("alarm_id"),
                    "due_at": record.get("due_at"),
                }
            )
        document.runtime.active = None

    def start_ringing(self, alarm: Alarm) -> bool:
        """Compatibility command that durably admits an immediate occurrence."""
        now = self._clock()
        occurrence = Occurrence(
            occurrence_id=f"manual:{alarm.id}:{uuid.uuid4()}",
            alarm_id=alarm.id,
            kind="scheduled",
            due_at=now,
            accepted_at=None,
            alarm=AlarmSnapshot.from_alarm(alarm),
        )
        return self.admit_occurrences((occurrence,), (), now) == 1

    def dismiss(self) -> bool:
        """Stop the currently-ringing alarm. Returns True if one was ringing."""
        with self._lock:
            now = self._clock()
            previous = None
            promoted = None

            def dismiss_active(document):
                nonlocal previous, promoted
                previous = document.runtime.active
                if previous is None:
                    return
                document.runtime.active = None
                self._promote_valid(document, now)
                promoted = document.runtime.active

            self.repository.transaction(dismiss_active)
            if previous is None:
                return False
            self.state.load()
            self.audio.stop()
            self._kill_popup()
            if promoted is not None:
                self._start_active_effects(promoted)
            logger.info("Alarm '%s' dismissed", previous["label"])
            return True

    def snooze(self, minutes: Optional[int] = None) -> bool:
        """Snooze the currently-ringing alarm. Returns True if one was ringing."""
        with self._lock:
            mins = self.settings.default_snooze_minutes if minutes is None else minutes
            if type(mins) is not int or not SNOOZE_MIN_MINUTES <= mins <= SNOOZE_MAX_MINUTES:
                return False
            now = self._clock()
            previous = None
            promoted = None

            def schedule(document):
                nonlocal previous, promoted
                active = document.runtime.active
                if active is None:
                    return
                alarm = next(
                    (item for item in document.alarms if item.id == active["alarm_id"]),
                    None,
                )
                if alarm is None or not alarm.enabled or not alarm.snoozable:
                    return
                previous = active
                due_at = now + timedelta(minutes=mins)
                snooze = dict(active)
                snooze.update(
                    occurrence_id=f"snooze:{alarm.id}:{uuid.uuid4()}",
                    kind="snooze",
                    due_at=due_at.isoformat(),
                    accepted_at=None,
                    alarm=alarm.to_dict(),
                )
                document.runtime.snoozes.append(snooze)
                document.runtime.active = None
                self._promote_valid(document, now)
                promoted = document.runtime.active

            self.repository.transaction(schedule)
            if previous is None:
                return False
            self.state.load()
            self.audio.stop()
            self._kill_popup()
            if promoted is not None:
                self._start_active_effects(promoted)
            logger.info("Alarm '%s' snoozed %s min", previous["label"], mins)
            return True

    def disable_alarm(self, alarm_id: str) -> bool:
        with self._lock:
            changed = self.repository.disable_alarm(alarm_id)
            self.alarms = self.repository.snapshot().alarms
            self.state.load()
            return changed

    def delete_alarm(self, alarm_id: str) -> bool:
        with self._lock:
            changed = self.repository.delete_alarm(alarm_id)
            self.alarms = self.repository.snapshot().alarms
            self.state.load()
            return changed

    def recover(self) -> bool:
        """Recover recent durable active/queued work once per daemon instance."""
        with self._lock:
            if self._recovered:
                return False
            self._recovered = True
            now = self._clock()
            active = None

            def classify(document):
                nonlocal active
                runtime = document.runtime
                records = ([runtime.active] if runtime.active else []) + list(runtime.queue)
                valid = []
                for record in records:
                    due_at = datetime.fromisoformat(record["due_at"])
                    age = now.astimezone(datetime_timezone.utc) - due_at.astimezone(
                        datetime_timezone.utc
                    )
                    if age > timedelta(minutes=CATCH_UP_MINUTES):
                        code = "recovery_stale"
                    else:
                        code = self._eligibility_code(document, record)
                    if code:
                        runtime.diagnostics.append(
                            {
                                "code": code,
                                "observed_at": now.isoformat(),
                                "occurrence_id": record.get("occurrence_id"),
                                "alarm_id": record.get("alarm_id"),
                                "due_at": record.get("due_at"),
                            }
                        )
                    else:
                        valid.append(record)
                runtime.active = valid[0] if valid else None
                runtime.queue = valid[1:]
                active = runtime.active

            self.repository.transaction(classify)
            self.state.load()
            if active is not None:
                self._start_active_effects(active)
                return True
            return False

    def test(self, alarm_id: str) -> bool:
        """Fire an alarm immediately for testing."""
        alarm = next(
            (
                item
                for item in self.repository.snapshot().alarms
                if item.id == alarm_id
            ),
            None,
        )
        if not alarm:
            return False
        return self.start_ringing(alarm)

    # ---- popup subprocess ----

    def _spawn_popup(self, alarm: Alarm) -> None:
        if not os.environ.get("DISPLAY"):
            logger.debug("No DISPLAY; skipping popup (web control only)")
            return
        cmd = [sys.executable, "-m", "alarm.popup",
               "--port", str(self.settings.web_port),
               "--label", alarm.label, "--time", alarm.time,
               "--snooze-minutes", str(self.settings.default_snooze_minutes)]
        if alarm.snoozable:
            cmd.append("--snoozable")
        if alarm.irritable:
            cmd.append("--irritable")
        try:
            self._popup_proc = subprocess.Popen(cmd, preexec_fn=os.setsid)
        except Exception as e:
            logger.warning(f"Could not spawn popup: {e}")
            self._popup_proc = None

    def _kill_popup(self) -> None:
        if not self._popup_proc:
            return
        try:
            os.killpg(os.getpgid(self._popup_proc.pid), signal.SIGTERM)
            self._popup_proc.wait(timeout=2.0)
        except (ProcessLookupError, OSError):
            pass
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._popup_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        finally:
            self._popup_proc = None


def run_daemon() -> None:
    """Entry point for running the daemon."""
    AlarmDaemon().run()
