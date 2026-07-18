"""Strict validation for untrusted alarm and settings payloads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


LABEL_MAX_LENGTH = 120

ALARM_FIELDS = (
    "id",
    "label",
    "time",
    "days_of_week",
    "enabled",
    "snoozable",
    "irritable",
    "sound_path",
    "base_volume",
    "irritable_duration_minutes",
    "irritable_volume_step",
    "skip_dates",
)

SETTINGS_FIELDS = (
    "audio_player",
    "audio_player_args",
    "default_sound",
    "default_snooze_minutes",
    "check_interval_seconds",
    "max_volume",
    "web_enabled",
    "web_host",
    "web_port",
    "web_pin",
)


@dataclass(frozen=True)
class FieldIssue:
    """One deterministic validation issue associated with a field path."""

    field: str
    code: str
    message: str


class ValidationError(ValueError):
    """Aggregate error raised only after a complete payload has been checked."""

    def __init__(self, issues: Sequence[FieldIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{item.field}: {item.message}" for item in self.issues))


def _issue(issues: list[FieldIssue], field: str, code: str, message: str) -> None:
    issues.append(FieldIssue(field, code, message))


def _mapping(payload: Any) -> tuple[Mapping[str, Any] | None, list[FieldIssue]]:
    if not isinstance(payload, Mapping):
        return None, [FieldIssue("$", "type", "must be an object")]
    return payload, []


def _unknown_and_missing(
    payload: Mapping[str, Any],
    fields: tuple[str, ...],
    issues: list[FieldIssue],
    *,
    partial: bool,
) -> None:
    if not partial:
        for field in fields:
            if field not in payload:
                _issue(issues, field, "required", "is required")
    unknown = sorted(
        (key for key in payload if key not in fields),
        key=lambda key: (type(key).__name__, repr(key)),
    )
    for field in unknown:
        _issue(issues, str(field), "unknown", "is not a recognized field")


def _ordered(issues: list[FieldIssue], fields: tuple[str, ...]) -> list[FieldIssue]:
    """Return issues in canonical field-path order, with unknowns last."""

    positions = {field: index for index, field in enumerate(fields)}

    def key(item: FieldIssue) -> tuple[int, str]:
        root = item.field.split("[", 1)[0]
        if root == "$":
            return (-1, item.field)
        return (positions.get(root, len(fields)), item.field)

    return sorted(issues, key=key)


def _text(value: Any, field: str, issues: list[FieldIssue], *, nonempty: bool = True) -> str | None:
    if type(value) is not str:
        _issue(issues, field, "type", "must be a string")
        return None
    normalized = value.strip()
    if nonempty and not normalized:
        _issue(issues, field, "empty", "must not be empty")
        return None
    return normalized


def _strict_int(
    value: Any,
    field: str,
    issues: list[FieldIssue],
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if type(value) is not int:
        _issue(issues, field, "type", "must be an integer")
        return None
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        if minimum is not None and maximum is not None:
            message = f"must be between {minimum} and {maximum}"
        elif minimum is not None:
            message = f"must be at least {minimum}"
        else:
            message = f"must be at most {maximum}"
        _issue(issues, field, "range", message)
        return None
    return value


def _strict_bool(value: Any, field: str, issues: list[FieldIssue]) -> bool | None:
    if type(value) is not bool:
        _issue(issues, field, "type", "must be a boolean")
        return None
    return value


def _time(value: Any, field: str, issues: list[FieldIssue]) -> str | None:
    text = _text(value, field, issues)
    if text is None:
        return None
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError:
        _issue(issues, field, "format", "must use HH:MM in 24-hour time")
        return None
    normalized = parsed.strftime("%H:%M")
    if text != normalized:
        _issue(issues, field, "format", "must use zero-padded HH:MM")
        return None
    return normalized


def _iso_date(value: Any, field: str, issues: list[FieldIssue]) -> str | None:
    text = _text(value, field, issues)
    if text is None:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        _issue(issues, field, "format", "must be a real ISO date (YYYY-MM-DD)")
        return None
    if parsed.isoformat() != text:
        _issue(issues, field, "format", "must use YYYY-MM-DD")
        return None
    return text


def _readable_file(value: Any, field: str, issues: list[FieldIssue], *, nullable: bool) -> str | None:
    if value is None and nullable:
        return None
    text = _text(value, field, issues)
    if text is None:
        return None
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        _issue(issues, field, "path", "must name an existing readable regular file")
        return None
    if not path.is_file() or not os.access(path, os.R_OK):
        _issue(issues, field, "path", "must name an existing readable regular file")
        return None
    return str(path)


def validate_alarm_payload(payload: Any, partial: bool = False) -> dict[str, Any]:
    """Validate and normalize an alarm payload, reporting every field issue."""

    data, issues = _mapping(payload)
    if data is None:
        raise ValidationError(issues)
    _unknown_and_missing(data, ALARM_FIELDS, issues, partial=partial)
    normalized: dict[str, Any] = {}

    if "id" in data:
        value = _text(data["id"], "id", issues)
        if value is not None:
            normalized["id"] = value
    if "label" in data:
        value = _text(data["label"], "label", issues)
        if value is not None:
            if len(value) > LABEL_MAX_LENGTH:
                _issue(issues, "label", "length", f"must be at most {LABEL_MAX_LENGTH} characters")
            else:
                normalized["label"] = value
    if "time" in data:
        value = _time(data["time"], "time", issues)
        if value is not None:
            normalized["time"] = value
    if "days_of_week" in data:
        raw_days = data["days_of_week"]
        if type(raw_days) is not list:
            _issue(issues, "days_of_week", "type", "must be a list")
        elif not raw_days:
            _issue(issues, "days_of_week", "empty", "must contain at least one weekday")
        else:
            days: list[int] = []
            for index, raw_day in enumerate(raw_days):
                value = _strict_int(raw_day, f"days_of_week[{index}]", issues, minimum=0, maximum=6)
                if value is not None:
                    days.append(value)
            if len(days) == len(raw_days):
                normalized["days_of_week"] = sorted(set(days))
    for field in ("enabled", "snoozable", "irritable"):
        if field in data:
            value = _strict_bool(data[field], field, issues)
            if value is not None:
                normalized[field] = value
    if "sound_path" in data:
        before = len(issues)
        value = _readable_file(data["sound_path"], "sound_path", issues, nullable=True)
        if len(issues) == before:
            normalized["sound_path"] = value
    for field, minimum, maximum in (
        ("base_volume", 0, 100),
        ("irritable_duration_minutes", 1, None),
        ("irritable_volume_step", 1, None),
    ):
        if field in data:
            value = _strict_int(data[field], field, issues, minimum=minimum, maximum=maximum)
            if value is not None:
                normalized[field] = value
    if "skip_dates" in data:
        raw_dates = data["skip_dates"]
        if type(raw_dates) is not list:
            _issue(issues, "skip_dates", "type", "must be a list")
        else:
            dates: list[str] = []
            for index, raw_date in enumerate(raw_dates):
                value = _iso_date(raw_date, f"skip_dates[{index}]", issues)
                if value is not None:
                    dates.append(value)
            if len(dates) == len(raw_dates):
                normalized["skip_dates"] = sorted(set(dates))

    if issues:
        raise ValidationError(_ordered(issues, ALARM_FIELDS))
    return normalized


def validate_settings_payload(payload: Any, partial: bool = False) -> dict[str, Any]:
    """Validate and normalize a settings payload, reporting every field issue."""

    data, issues = _mapping(payload)
    if data is None:
        raise ValidationError(issues)
    _unknown_and_missing(data, SETTINGS_FIELDS, issues, partial=partial)
    normalized: dict[str, Any] = {}

    for field in ("audio_player", "web_host"):
        if field in data:
            value = _text(data[field], field, issues)
            if value is not None:
                normalized[field] = value
    if "audio_player_args" in data:
        raw_args = data["audio_player_args"]
        if type(raw_args) is not list:
            _issue(issues, "audio_player_args", "type", "must be a list")
        else:
            args: list[str] = []
            for index, raw_arg in enumerate(raw_args):
                if type(raw_arg) is not str:
                    _issue(issues, f"audio_player_args[{index}]", "type", "must be a string")
                else:
                    args.append(raw_arg)
            if len(args) == len(raw_args):
                normalized["audio_player_args"] = args
    if "default_sound" in data:
        before = len(issues)
        value = _readable_file(data["default_sound"], "default_sound", issues, nullable=False)
        if len(issues) == before:
            normalized["default_sound"] = value
    for field, minimum, maximum in (
        ("default_snooze_minutes", 1, None),
        ("check_interval_seconds", 1, None),
        ("max_volume", 0, 100),
        ("web_port", 1, 65535),
    ):
        if field in data:
            value = _strict_int(data[field], field, issues, minimum=minimum, maximum=maximum)
            if value is not None:
                normalized[field] = value
    if "web_enabled" in data:
        value = _strict_bool(data["web_enabled"], "web_enabled", issues)
        if value is not None:
            normalized["web_enabled"] = value
    if "web_pin" in data:
        value = data["web_pin"]
        if value is None:
            normalized["web_pin"] = None
        elif type(value) is not str:
            _issue(issues, "web_pin", "type", "must be a string or null")
        else:
            normalized["web_pin"] = value

    if issues:
        raise ValidationError(_ordered(issues, SETTINGS_FIELDS))
    return normalized


__all__ = [
    "FieldIssue",
    "ValidationError",
    "LABEL_MAX_LENGTH",
    "validate_alarm_payload",
    "validate_settings_payload",
]
