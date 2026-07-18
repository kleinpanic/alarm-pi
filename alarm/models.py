"""
Data models for alarms and settings.

Uses Python dataclasses for clean, type-hinted structures.
days_of_week uses 0-6 where 0=Monday, 6=Sunday (matches Python's weekday()).
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Mapping, Optional


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
    def from_payload(cls, data: Mapping[str, Any]) -> "Alarm":
        """Construct from an untrusted current-schema payload after strict validation."""
        from alarm.validation import validate_alarm_payload

        return cls(**validate_alarm_payload(data))

    @classmethod
    def from_dict(cls, data: dict) -> "Alarm":
        """Create Alarm from a legacy dictionary pending schema migration."""
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


@dataclass(frozen=True)
class AlarmSnapshot:
    """Immutable alarm values captured when an occurrence is generated."""

    id: str
    label: str
    time: str
    days_of_week: tuple[int, ...]
    enabled: bool
    snoozable: bool
    irritable: bool
    sound_path: Optional[str]
    base_volume: int
    irritable_duration_minutes: int
    irritable_volume_step: int
    skip_dates: tuple[str, ...]

    @classmethod
    def from_alarm(cls, alarm: Alarm) -> "AlarmSnapshot":
        return cls(
            id=alarm.id,
            label=alarm.label,
            time=alarm.time,
            days_of_week=tuple(alarm.days_of_week),
            enabled=alarm.enabled,
            snoozable=alarm.snoozable,
            irritable=alarm.irritable,
            sound_path=alarm.sound_path,
            base_volume=alarm.base_volume,
            irritable_duration_minutes=alarm.irritable_duration_minutes,
            irritable_volume_step=alarm.irritable_volume_step,
            skip_dates=tuple(alarm.skip_dates),
        )


@dataclass(frozen=True)
class Occurrence:
    """A pure scheduler candidate awaiting durable admission by the daemon."""

    occurrence_id: str
    alarm_id: str
    kind: str
    due_at: datetime
    accepted_at: Optional[datetime]
    alarm: AlarmSnapshot


@dataclass(frozen=True)
class SchedulerDiagnostic:
    """Stable explanation for a due slot that cannot be emitted normally."""

    code: str
    observed_at: datetime
    occurrence_id: Optional[str] = None
    alarm_id: Optional[str] = None
    due_at: Optional[datetime] = None
    scheduled_for: Optional[str] = None


@dataclass(frozen=True)
class SchedulerEvaluation:
    """Pure interval result; callers decide how and when to persist it."""

    occurrences: tuple[Occurrence, ...]
    diagnostics: tuple[SchedulerDiagnostic, ...]
    checkpoint: datetime


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
    def from_payload(cls, data: Mapping[str, Any]) -> "Settings":
        """Construct from an untrusted current-schema payload after strict validation."""
        from alarm.validation import validate_settings_payload

        return cls(**validate_settings_payload(data))

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        """Create Settings from a legacy dictionary pending schema migration."""
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


RUNTIME_FIELDS = (
    "scheduler_checkpoint",
    "active",
    "queue",
    "snoozes",
    "accepted_occurrences",
    "diagnostics",
)


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    """Return a detached JSON object or reject non-JSON runtime values."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be an object")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError(f"{field_name} keys must be strings")
        if item is None or type(item) in (str, int, float, bool):
            result[key] = item
        elif isinstance(item, Mapping):
            result[key] = _json_object(item, f"{field_name}.{key}")
        elif type(item) is list:
            values = []
            for index, nested in enumerate(item):
                if isinstance(nested, Mapping):
                    values.append(_json_object(nested, f"{field_name}.{key}[{index}]"))
                elif nested is None or type(nested) in (str, int, float, bool):
                    values.append(nested)
                else:
                    raise ValueError(f"{field_name}.{key}[{index}] is not JSON-safe")
            result[key] = values
        else:
            raise ValueError(f"{field_name}.{key} is not JSON-safe")
    return result


@dataclass
class RuntimeData:
    """Validated durable runtime aggregate used by the operational document."""

    scheduler_checkpoint: Optional[str] = None
    active: Optional[dict[str, Any]] = None
    queue: List[dict[str, Any]] = field(default_factory=list)
    snoozes: List[dict[str, Any]] = field(default_factory=list)
    accepted_occurrences: List[str] = field(default_factory=list)
    diagnostics: List[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scheduler_checkpoint": self.scheduler_checkpoint,
            "active": None if self.active is None else _json_object(self.active, "active"),
            "queue": [_json_object(item, f"queue[{index}]") for index, item in enumerate(self.queue)],
            "snoozes": [
                _json_object(item, f"snoozes[{index}]") for index, item in enumerate(self.snoozes)
            ],
            "accepted_occurrences": list(self.accepted_occurrences),
            "diagnostics": [
                _json_object(item, f"diagnostics[{index}]")
                for index, item in enumerate(self.diagnostics)
            ],
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "RuntimeData":
        if not isinstance(payload, Mapping):
            raise ValueError("runtime must be an object")
        missing = [name for name in RUNTIME_FIELDS if name not in payload]
        unknown = [name for name in payload if name not in RUNTIME_FIELDS]
        if missing or unknown:
            raise ValueError("runtime fields do not match schema version 1")

        checkpoint = payload["scheduler_checkpoint"]
        if checkpoint is not None and type(checkpoint) is not str:
            raise ValueError("runtime.scheduler_checkpoint must be a string or null")
        active = payload["active"]
        if active is not None:
            active = _json_object(active, "runtime.active")

        collections: dict[str, list[Any]] = {}
        for name in ("queue", "snoozes", "accepted_occurrences", "diagnostics"):
            value = payload[name]
            if type(value) is not list:
                raise ValueError(f"runtime.{name} must be a list")
            collections[name] = value

        accepted = collections["accepted_occurrences"]
        if any(type(item) is not str for item in accepted):
            raise ValueError("runtime.accepted_occurrences entries must be strings")
        return cls(
            scheduler_checkpoint=checkpoint,
            active=active,
            queue=[_json_object(item, f"runtime.queue[{i}]") for i, item in enumerate(collections["queue"])],
            snoozes=[
                _json_object(item, f"runtime.snoozes[{i}]")
                for i, item in enumerate(collections["snoozes"])
            ],
            accepted_occurrences=list(accepted),
            diagnostics=[
                _json_object(item, f"runtime.diagnostics[{i}]")
                for i, item in enumerate(collections["diagnostics"])
            ],
        )


@dataclass
class OperationalDocument:
    """Schema-versioned aggregate loaded only after complete validation."""

    schema_version: int
    revision: int
    alarms: List[Alarm]
    settings: Settings
    runtime: RuntimeData

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "alarms": [alarm.to_dict() for alarm in self.alarms],
            "settings": self.settings.to_dict(),
            "runtime": self.runtime.to_dict(),
        }

    @classmethod
    def from_payload(cls, payload: Any) -> "OperationalDocument":
        if not isinstance(payload, Mapping):
            raise ValueError("operational document must be an object")
        expected = {"schema_version", "revision", "alarms", "settings", "runtime"}
        if set(payload) != expected:
            raise ValueError("operational document fields do not match schema version 1")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise ValueError("unsupported operational schema version")
        if type(payload["revision"]) is not int or payload["revision"] < 0:
            raise ValueError("revision must be a non-negative integer")
        if type(payload["alarms"]) is not list:
            raise ValueError("alarms must be a list")
        alarms = [Alarm.from_payload(item) for item in payload["alarms"]]
        settings = Settings.from_payload(payload["settings"])
        runtime = RuntimeData.from_payload(payload["runtime"])
        return cls(1, payload["revision"], alarms, settings, runtime)


__all__ = [
    "Alarm",
    "AlarmSnapshot",
    "Occurrence",
    "SchedulerDiagnostic",
    "SchedulerEvaluation",
    "Settings",
    "RuntimeData",
    "OperationalDocument",
]
