from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def test_alarm_validation_aggregates_empty_null_single_and_multiple_issues():
    from alarm.validation import ValidationError, validate_alarm_payload

    with pytest.raises(ValidationError) as empty:
        validate_alarm_payload({})
    assert [issue.field for issue in empty.value.issues] == [
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
    ]
    assert {issue.code for issue in empty.value.issues} == {"required"}

    with pytest.raises(ValidationError) as null_label:
        validate_alarm_payload(_valid_alarm(label=None))
    assert [(issue.field, issue.code) for issue in null_label.value.issues] == [
        ("label", "type")
    ]

    with pytest.raises(ValidationError) as one:
        validate_alarm_payload(_valid_alarm(time="25:00"))
    assert [(issue.field, issue.code) for issue in one.value.issues] == [
        ("time", "format")
    ]

    with pytest.raises(ValidationError) as many:
        validate_alarm_payload(_valid_alarm(label=" ", time="nope", base_volume=101))
    assert [(issue.field, issue.code) for issue in many.value.issues] == [
        ("label", "empty"),
        ("time", "format"),
        ("base_volume", "range"),
    ]


def _valid_alarm(**overrides):
    payload = {
        "id": "alarm-1",
        "label": "Wake up",
        "time": "07:30",
        "days_of_week": [0, 1, 2, 3, 4],
        "enabled": True,
        "snoozable": True,
        "irritable": False,
        "sound_path": None,
        "base_volume": 70,
        "irritable_duration_minutes": 5,
        "irritable_volume_step": 10,
        "skip_dates": [],
    }
    payload.update(overrides)
    return payload


def _valid_settings(sound_path: Path, **overrides):
    payload = {
        "audio_player": "mpv",
        "audio_player_args": ["--no-video", "--loop=inf"],
        "default_sound": str(sound_path),
        "default_snooze_minutes": 5,
        "check_interval_seconds": 5,
        "max_volume": 100,
        "web_enabled": True,
        "web_host": "127.0.0.1",
        "web_port": 8765,
        "web_pin": None,
    }
    payload.update(overrides)
    return payload


def test_alarm_and_settings_share_the_strict_validation_boundary(tmp_path):
    from alarm.validation import ValidationError, validate_alarm_payload, validate_settings_payload

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    assert validate_alarm_payload(_valid_alarm())["time"] == "07:30"
    assert validate_settings_payload(_valid_settings(sound))["web_port"] == 8765

    for validator in (validate_alarm_payload, validate_settings_payload):
        with pytest.raises(ValidationError) as raised:
            validator(None)
        assert [(issue.field, issue.code) for issue in raised.value.issues] == [("$", "type")]


def test_validation_error_issues_are_frozen_and_immutable():
    from alarm.validation import ValidationError, validate_alarm_payload

    with pytest.raises(ValidationError) as raised:
        validate_alarm_payload(_valid_alarm(label="", time="99:99"))
    assert isinstance(raised.value.issues, tuple)
    with pytest.raises(FrozenInstanceError):
        raised.value.issues[0].code = "changed"


def test_unknown_fields_are_rejected_after_canonical_field_issues_in_stable_order():
    from alarm.validation import ValidationError, validate_alarm_payload

    payload = _valid_alarm(label="", base_volume=101)
    payload["zzz"] = True
    payload["aaa"] = True

    observed = []
    for _ in range(3):
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(payload)
        observed.append([(issue.field, issue.code) for issue in raised.value.issues])

    assert observed == [
        [
            ("label", "empty"),
            ("base_volume", "range"),
            ("aaa", "unknown"),
            ("zzz", "unknown"),
        ]
    ] * 3


def test_labels_are_trimmed_nonempty_and_limited_to_120_characters():
    from alarm.validation import LABEL_MAX_LENGTH, ValidationError, validate_alarm_payload

    assert LABEL_MAX_LENGTH == 120
    assert validate_alarm_payload(_valid_alarm(label="  Wake up  "))["label"] == "Wake up"
    assert validate_alarm_payload(_valid_alarm(label="x" * 120))["label"] == "x" * 120
    for label, code in (("   ", "empty"), ("x" * 121, "length")):
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(_valid_alarm(label=label))
        assert [(issue.field, issue.code) for issue in raised.value.issues] == [("label", code)]


def test_sound_paths_are_resolved_and_must_be_readable_regular_files(tmp_path, monkeypatch):
    from alarm.validation import ValidationError, validate_alarm_payload

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    assert validate_alarm_payload(_valid_alarm(sound_path=str(sound)))["sound_path"] == str(sound.resolve())

    for invalid in (tmp_path / "missing.wav", tmp_path):
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(_valid_alarm(sound_path=str(invalid)))
        assert [(issue.field, issue.code) for issue in raised.value.issues] == [("sound_path", "path")]

    monkeypatch.setattr("alarm.validation.os.access", lambda path, mode: False)
    with pytest.raises(ValidationError) as unreadable:
        validate_alarm_payload(_valid_alarm(sound_path=str(sound)))
    assert [(issue.field, issue.code) for issue in unreadable.value.issues] == [("sound_path", "path")]


@pytest.mark.parametrize("field", ["enabled", "snoozable", "irritable"])
@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_boolean_fields_reject_truthiness(field, value):
    from alarm.validation import ValidationError, validate_alarm_payload

    with pytest.raises(ValidationError) as raised:
        validate_alarm_payload(_valid_alarm(**{field: value}))
    assert [(issue.field, issue.code) for issue in raised.value.issues] == [(field, "type")]


@pytest.mark.parametrize(
    ("field", "valid_values", "invalid_values"),
    [
        ("base_volume", [0, 100], [-1, 101, True, 70.5, "70"]),
        ("irritable_duration_minutes", [1], [0, -1, True, 1.5, "1"]),
        ("irritable_volume_step", [1], [0, -1, True, 1.5, "1"]),
    ],
)
def test_alarm_integer_boundaries_and_precision(field, valid_values, invalid_values):
    from alarm.validation import ValidationError, validate_alarm_payload

    for value in valid_values:
        assert validate_alarm_payload(_valid_alarm(**{field: value}))[field] == value
    for value in invalid_values:
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(_valid_alarm(**{field: value}))
        assert raised.value.issues[0].field == field


def test_weekdays_are_strict_sorted_unique_and_bounded():
    from alarm.validation import ValidationError, validate_alarm_payload

    assert validate_alarm_payload(_valid_alarm(days_of_week=[6, 0, 6]))["days_of_week"] == [0, 6]
    for days, field in (([-1], "days_of_week[0]"), ([7], "days_of_week[0]"), ([True], "days_of_week[0]"), ([1.0], "days_of_week[0]")):
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(_valid_alarm(days_of_week=days))
        assert raised.value.issues[0].field == field


@pytest.mark.parametrize("valid", ["00:00", "23:59"])
def test_time_inclusive_boundaries(valid):
    from alarm.validation import validate_alarm_payload

    assert validate_alarm_payload(_valid_alarm(time=valid))["time"] == valid


@pytest.mark.parametrize("invalid", ["24:00", "23:60", "7:30", "07:30:00", None])
def test_time_adjacent_and_type_failures(invalid):
    from alarm.validation import ValidationError, validate_alarm_payload

    with pytest.raises(ValidationError) as raised:
        validate_alarm_payload(_valid_alarm(time=invalid))
    assert raised.value.issues[0].field == "time"


def test_skip_dates_require_real_iso_dates_and_normalize_uniquely():
    from alarm.validation import ValidationError, validate_alarm_payload

    normalized = validate_alarm_payload(_valid_alarm(skip_dates=["2026-12-31", "2026-01-01", "2026-12-31"]))
    assert normalized["skip_dates"] == ["2026-01-01", "2026-12-31"]
    for invalid in ("2026-02-29", "2026-13-01", "01-01-2026", None):
        with pytest.raises(ValidationError) as raised:
            validate_alarm_payload(_valid_alarm(skip_dates=[invalid]))
        assert raised.value.issues[0].field == "skip_dates[0]"


@pytest.mark.parametrize(
    ("field", "valid_values", "invalid_values"),
    [
        ("web_port", [1, 65535], [0, 65536, True, 8765.0, "8765"]),
        ("max_volume", [0, 100], [-1, 101, True, 50.5, "50"]),
        ("default_snooze_minutes", [1], [0, -1, True, 1.5, "1"]),
        ("check_interval_seconds", [1], [0, -1, True, 1.5, "1"]),
    ],
)
def test_settings_integer_boundaries_and_precision(tmp_path, field, valid_values, invalid_values):
    from alarm.validation import ValidationError, validate_settings_payload

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    for value in valid_values:
        assert validate_settings_payload(_valid_settings(sound, **{field: value}))[field] == value
    for value in invalid_values:
        with pytest.raises(ValidationError) as raised:
            validate_settings_payload(_valid_settings(sound, **{field: value}))
        assert raised.value.issues[0].field == field


def test_partial_validation_checks_only_present_fields_but_still_rejects_unknowns():
    from alarm.validation import ValidationError, validate_alarm_payload

    assert validate_alarm_payload({"label": "  Updated  "}, partial=True) == {"label": "Updated"}
    with pytest.raises(ValidationError) as raised:
        validate_alarm_payload({"typo": 1}, partial=True)
    assert [(issue.field, issue.code) for issue in raised.value.issues] == [("typo", "unknown")]


def test_validation_is_pure_and_does_not_change_operational_configuration():
    from alarm import config
    from alarm.validation import validate_alarm_payload

    before = {
        path: path.read_bytes() if path.exists() else None
        for path in (config.ALARMS_FILE, config.SETTINGS_FILE, config.STATE_FILE)
    }
    normalized = validate_alarm_payload(_valid_alarm(enabled=False))
    after = {
        path: path.read_bytes() if path.exists() else None
        for path in (config.ALARMS_FILE, config.SETTINGS_FILE, config.STATE_FILE)
    }
    assert normalized["enabled"] is False
    assert after == before


def test_alarm_model_from_payload_constructs_only_from_normalized_values(tmp_path):
    from alarm.models import Alarm

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    alarm = Alarm.from_payload(
        _valid_alarm(
            label="  Wake up  ",
            days_of_week=[6, 0, 6],
            sound_path=str(sound),
        )
    )
    assert alarm.label == "Wake up"
    assert alarm.days_of_week == [0, 6]
    assert alarm.sound_path == str(sound.resolve())
    assert alarm.to_dict() == _valid_alarm(
        label="Wake up",
        days_of_week=[0, 6],
        sound_path=str(sound.resolve()),
    )


def test_settings_model_from_payload_constructs_only_from_normalized_values(tmp_path):
    from alarm.models import Settings

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    settings = Settings.from_payload(_valid_settings(sound, web_host=" 127.0.0.1 "))
    assert settings.web_host == "127.0.0.1"
    assert settings.default_sound == str(sound.resolve())
    assert settings.to_dict() == _valid_settings(sound.resolve())


def test_model_payload_factories_reject_before_partial_construction(tmp_path):
    from alarm.models import Alarm, Settings
    from alarm.validation import ValidationError

    sound = tmp_path / "tone.wav"
    sound.write_bytes(b"sound")
    with pytest.raises(ValidationError) as alarm_error:
        Alarm.from_payload(_valid_alarm(label="", enabled=1))
    assert [(issue.field, issue.code) for issue in alarm_error.value.issues] == [
        ("label", "empty"),
        ("enabled", "type"),
    ]
    with pytest.raises(ValidationError) as settings_error:
        Settings.from_payload(_valid_settings(sound, web_port="8765", surprise=True))
    assert [(issue.field, issue.code) for issue in settings_error.value.issues] == [
        ("web_port", "type"),
        ("surprise", "unknown"),
    ]
