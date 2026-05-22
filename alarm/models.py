"""
Data models for alarms and settings.

Uses Python dataclasses for clean, type-hinted structures.
days_of_week uses 0-6 where 0=Monday, 6=Sunday (matches Python's weekday()).
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Alarm:
    """Represents a single alarm configuration."""
    id: str
    label: str
    time: str  # "HH:MM" 24-hour format
    days_of_week: List[int]  # 0=Monday, 6=Sunday
    enabled: bool = True
    snoozable: bool = True
    irritable: bool = False
    sound_path: Optional[str] = None  # None means use default
    base_volume: int = 70  # 0-100 scale for system mixer
    irritable_duration_minutes: int = 5
    irritable_volume_step: int = 10  # Increase by this much each minute
    skip_dates: List[str] = field(default_factory=list)  # "YYYY-MM-DD" format

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "id": self.id,
            "label": self.label,
            "time": self.time,
            "days_of_week": self.days_of_week,
            "enabled": self.enabled,
            "snoozable": self.snoozable,
            "irritable": self.irritable,
            "sound_path": self.sound_path,
            "base_volume": self.base_volume,
            "irritable_duration_minutes": self.irritable_duration_minutes,
            "irritable_volume_step": self.irritable_volume_step,
            "skip_dates": self.skip_dates,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Alarm":
        """Create Alarm from dictionary."""
        return cls(
            id=str(data.get("id", "")),
            label=data.get("label", "Unnamed Alarm"),
            time=data.get("time", "07:00"),
            days_of_week=data.get("days_of_week", [0, 1, 2, 3, 4]),
            enabled=data.get("enabled", True),
            snoozable=data.get("snoozable", True),
            irritable=data.get("irritable", False),
            sound_path=data.get("sound_path"),
            base_volume=data.get("base_volume", 70),
            irritable_duration_minutes=data.get("irritable_duration_minutes", 5),
            irritable_volume_step=data.get("irritable_volume_step", 10),
            skip_dates=data.get("skip_dates", []),
        )


@dataclass
class Settings:
    """Global application settings."""
    audio_player: str = "mpv"
    audio_player_args: List[str] = field(default_factory=lambda: ["--no-video", "--loop=inf"])
    default_sound: str = "/usr/share/sounds/alsa/Front_Center.wav"
    default_snooze_minutes: int = 5
    check_interval_seconds: int = 5
    max_volume: int = 100  # Cap for irritable mode escalation
    # Web UI
    web_enabled: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 8765
    web_pin: Optional[str] = None  # None/"" = open on LAN; localhost always bypasses

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "audio_player": self.audio_player,
            "audio_player_args": self.audio_player_args,
            "default_sound": self.default_sound,
            "default_snooze_minutes": self.default_snooze_minutes,
            "check_interval_seconds": self.check_interval_seconds,
            "max_volume": self.max_volume,
            "web_enabled": self.web_enabled,
            "web_host": self.web_host,
            "web_port": self.web_port,
            "web_pin": self.web_pin,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Create Settings from dictionary."""
        return cls(
            audio_player=data.get("audio_player", "mpv"),
            audio_player_args=data.get("audio_player_args", ["--no-video", "--loop=inf"]),
            default_sound=data.get("default_sound", "/usr/share/sounds/alsa/Front_Center.wav"),
            default_snooze_minutes=data.get("default_snooze_minutes", 5),
            check_interval_seconds=data.get("check_interval_seconds", 5),
            max_volume=data.get("max_volume", 100),
            web_enabled=data.get("web_enabled", True),
            web_host=data.get("web_host", "0.0.0.0"),
            web_port=data.get("web_port", 8765),
            web_pin=data.get("web_pin") or None,
        )
