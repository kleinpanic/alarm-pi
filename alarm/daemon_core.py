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
from typing import Optional

from .config import (
    load_alarms, load_settings, get_config_mtime, get_alarm_by_id, ALARMS_FILE,
)
from .models import Alarm
from .scheduler import AlarmScheduler
from .audio import AudioPlayer
from .state import RuntimeState

logger = logging.getLogger(__name__)


class AlarmDaemon:
    """Runs the scheduling loop and hosts the web server."""

    def __init__(self):
        self.settings = load_settings()
        self.alarms = load_alarms()
        self.state = RuntimeState()
        self.scheduler = AlarmScheduler(state=self.state)
        self.audio = AudioPlayer(self.settings)

        self._running = False
        self._config_mtime = get_config_mtime()
        self._popup_proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._web_thread: Optional[threading.Thread] = None

        # A ringing flag persisted from a previous crash is stale on startup
        # (no audio is actually playing). Snoozes, however, remain valid.
        self.state.clear_ringing()

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

        while self._running:
            try:
                self._check_config_reload()
                for alarm in self.scheduler.check_alarms(self.alarms):
                    self.start_ringing(alarm)
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
            logger.info("Config change detected, reloading")
            new_settings = load_settings()
            self.alarms = load_alarms()
            # Keep the existing AudioPlayer if mid-ring; just refresh settings.
            self.settings = new_settings
            self.audio.settings = new_settings
            self._config_mtime = current
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

    def start_ringing(self, alarm: Alarm) -> bool:
        """Begin ringing an alarm. No-op if one is already ringing."""
        with self._lock:
            if self.state.get_ringing() is not None:
                logger.info(f"Alarm '{alarm.label}' fired but one is already ringing; ignoring")
                return False
            logger.info(f"Firing alarm: '{alarm.label}' (id={alarm.id})")
            self.state.set_ringing(alarm)
            if not self.audio.play(alarm):
                logger.error("Failed to start audio playback")
            self._spawn_popup(alarm)
            return True

    def dismiss(self) -> bool:
        """Stop the currently-ringing alarm. Returns True if one was ringing."""
        with self._lock:
            ringing = self.state.get_ringing()
            self.audio.stop()
            self._kill_popup()
            if ringing:
                self.scheduler.cancel_snooze(ringing.alarm_id)
                self.state.clear_ringing()
                logger.info(f"Alarm '{ringing.label}' dismissed")
                return True
            return False

    def snooze(self, minutes: Optional[int] = None) -> bool:
        """Snooze the currently-ringing alarm. Returns True if one was ringing."""
        with self._lock:
            ringing = self.state.get_ringing()
            if not ringing:
                return False
            self.audio.stop()
            self._kill_popup()
            mins = minutes or self.settings.default_snooze_minutes
            alarm = get_alarm_by_id(ringing.alarm_id) or Alarm(
                id=ringing.alarm_id, label=ringing.label,
                time=ringing.time, days_of_week=[],
            )
            self.scheduler.schedule_snooze(alarm, mins)
            self.state.clear_ringing()
            logger.info(f"Alarm '{ringing.label}' snoozed {mins} min")
            return True

    def test(self, alarm_id: str) -> bool:
        """Fire an alarm immediately for testing."""
        alarm = get_alarm_by_id(alarm_id)
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
