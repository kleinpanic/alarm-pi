"""
Audio playback and volume control.

Uses subprocess to call system audio players (mpv by default).
Volume control via amixer for irritable mode escalation.
"""

import subprocess
import os
import signal
import logging
from pathlib import Path
from typing import Optional, List
import threading
import time

from .models import Alarm, Settings

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Manages audio playback for alarms.
    
    Uses subprocess to invoke system audio player (mpv, ffplay, aplay).
    Handles irritable mode volume escalation via amixer.
    """
    
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: Optional[subprocess.Popen] = None
        self._irritable_thread: Optional[threading.Thread] = None
        self._stop_irritable = threading.Event()
        self._current_volume = 0
    
    def play(self, alarm: Alarm) -> bool:
        """
        Start playing alarm sound.
        
        Args:
            alarm: The alarm to play sound for
        
        Returns:
            True if playback started successfully
        """
        # Stop any existing playback first
        self.stop()

        # Determine sound file (relative paths resolve against the project root)
        sound_path = self._resolve(alarm.sound_path or self.settings.default_sound)

        if not Path(sound_path).exists():
            logger.warning(f"Sound file not found: {sound_path}")
            # Try default as fallback
            default = self._resolve(self.settings.default_sound)
            if sound_path != default:
                sound_path = default
                if not Path(sound_path).exists():
                    logger.error("Default sound file also not found")
                    return False
        
        # Set initial volume
        self._current_volume = alarm.base_volume
        self._set_system_volume(self._current_volume)
        
        # Build command
        cmd = self._build_player_command(sound_path)
        logger.debug(f"Playing audio: {' '.join(cmd)}")
        
        try:
            # Start player process
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid  # Create new process group for clean kill
            )
            
            # Start irritable mode escalation if enabled
            if alarm.irritable:
                self._start_irritable_mode(alarm)
            
            return True
        except FileNotFoundError:
            logger.error(f"Audio player not found: {self.settings.audio_player}")
            return False
        except Exception as e:
            logger.error(f"Failed to start audio: {e}")
            return False
    
    @staticmethod
    def _resolve(sound_path: str) -> str:
        """Resolve a relative sound path against the project root."""
        from .config import PROJECT_ROOT
        p = Path(sound_path)
        return str(p if p.is_absolute() else PROJECT_ROOT / p)

    def _build_player_command(self, sound_path: str) -> List[str]:
        """Build the command line for the audio player."""
        cmd = [self.settings.audio_player]
        cmd.extend(self.settings.audio_player_args)
        cmd.append(sound_path)
        return cmd
    
    def stop(self) -> None:
        """Stop all audio playback and irritable mode."""
        # Stop irritable mode thread
        self._stop_irritable.set()
        if self._irritable_thread and self._irritable_thread.is_alive():
            self._irritable_thread.join(timeout=1.0)
        self._irritable_thread = None
        self._stop_irritable.clear()
        
        # Kill audio process
        if self._process:
            try:
                # Kill the entire process group
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                self._process.wait(timeout=2.0)
            except (ProcessLookupError, OSError):
                pass  # Process already dead
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            finally:
                self._process = None
        
        logger.debug("Audio playback stopped")
    
    def is_playing(self) -> bool:
        """Check if audio is currently playing."""
        if self._process is None:
            return False
        return self._process.poll() is None
    
    def _set_system_volume(self, volume: int) -> None:
        """
        Set system volume using amixer.
        
        Volume is clamped to 0-100 range and capped at max_volume setting.
        """
        volume = max(0, min(volume, self.settings.max_volume))
        
        try:
            subprocess.run(
                ["amixer", "set", "Master", f"{volume}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5.0
            )
            logger.debug(f"System volume set to {volume}%")
        except FileNotFoundError:
            logger.warning("amixer not found, cannot adjust volume")
        except subprocess.TimeoutExpired:
            logger.warning("amixer timed out")
        except Exception as e:
            logger.warning(f"Failed to set volume: {e}")
    
    def _start_irritable_mode(self, alarm: Alarm) -> None:
        """Start the irritable mode escalation thread."""
        self._stop_irritable.clear()
        self._irritable_thread = threading.Thread(
            target=self._irritable_loop,
            args=(alarm,),
            daemon=True
        )
        self._irritable_thread.start()
        logger.info(f"Irritable mode started for alarm '{alarm.label}'")
    
    def _irritable_loop(self, alarm: Alarm) -> None:
        """
        Irritable mode escalation loop.
        
        Increases volume every minute for the configured duration.
        """
        escalations = 0
        
        while escalations < alarm.irritable_duration_minutes:
            # Wait 60 seconds or until stop signal
            if self._stop_irritable.wait(timeout=60.0):
                logger.debug("Irritable mode stopped by signal")
                return
            
            # Check if we should still be running
            if not self.is_playing():
                logger.debug("Audio stopped, ending irritable mode")
                return
            
            # Escalate volume
            escalations += 1
            new_volume = min(
                alarm.base_volume + (escalations * alarm.irritable_volume_step),
                self.settings.max_volume
            )
            
            self._current_volume = new_volume
            self._set_system_volume(new_volume)
            logger.info(f"Irritable escalation {escalations}/{alarm.irritable_duration_minutes}: "
                       f"volume now {new_volume}%")
        
        logger.info("Irritable mode completed all escalations")


def test_audio(settings: Settings) -> bool:
    """
    Quick test of audio system using speaker-test.
    Returns True if test completed without error.
    """
    try:
        result = subprocess.run(
            ["speaker-test", "-t", "sine", "-f", "440", "-l", "1"],
            timeout=3.0,
            capture_output=True
        )
        return result.returncode == 0
    except Exception as e:
        logger.error(f"Audio test failed: {e}")
        return False
