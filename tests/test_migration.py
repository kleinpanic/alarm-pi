import json
from pathlib import Path

import pytest

from alarm import config
from alarm.models import OperationalDocument
from alarm.repository import (
    MigrationError,
    OperationalRepository,
    QuarantineError,
    RepositoryError,
)


def _repo(tmp_path: Path, *, stamp: str = "20260717T120000000000Z") -> OperationalRepository:
    return OperationalRepository(tmp_path, timestamp=lambda: stamp)


def _legacy_alarm(**changes):
    payload = {
        "id": "7",
        "label": "School (stopped)",
        "time": "06:45",
        "days_of_week": [0, 1, 2, 3, 4],
        "enabled": False,
        "snoozable": False,
        "irritable": True,
        "sound_path": None,
        "base_volume": 63,
        "irritable_duration_minutes": 9,
        "irritable_volume_step": 4,
        "skip_dates": ["2026-07-20"],
    }
    payload.update(changes)
    return payload


def test_true_absence_initializes_v1_but_explicit_empty_remains_empty(tmp_path):
    repository = _repo(tmp_path)

    created = repository.load()
    assert created.schema_version == 1
    assert created.revision == 0
    assert created.alarms == []
    initial_bytes = repository.operational_file.read_bytes()

    loaded = repository.load()
    assert loaded == created
    assert repository.operational_file.read_bytes() == initial_bytes

    repository.operational_file.write_text(
        json.dumps({**created.to_dict(), "alarms": []}), encoding="utf-8"
    )
    assert repository.load().alarms == []


def test_migrates_all_supported_legacy_values_once_without_enabling_alarm(tmp_path):
    repository = _repo(tmp_path)
    repository.config_dir.mkdir(parents=True, exist_ok=True)
    alarm_bytes = json.dumps([_legacy_alarm()]).encode()
    settings_payload = {
        "audio_player": "vlc",
        "audio_player_args": ["--loop"],
        "default_sound": "/usr/share/sounds/alsa/Front_Center.wav",
        "default_snooze_minutes": 11,
        "check_interval_seconds": 3,
        "max_volume": 88,
        "web_enabled": True,
        "web_host": "127.0.0.1",
        "web_port": 9911,
        "web_pin": "super-secret-pin",
    }
    state_payload = {
        "ringing": {
            "alarm_id": "3",
            "label": "Existing ring",
            "time": "07:30",
            "snoozable": True,
            "irritable": False,
            "started_at": "2026-07-17T07:30:00",
        },
        "snoozes": {"7": "2026-07-17T07:40:00"},
    }
    repository.legacy_alarms_file.write_bytes(alarm_bytes)
    repository.legacy_settings_file.write_text(json.dumps(settings_payload), encoding="utf-8")
    repository.legacy_state_file.write_text(json.dumps(state_payload), encoding="utf-8")

    migrated = repository.load()
    assert migrated.alarms[0].to_dict() == _legacy_alarm()
    assert migrated.alarms[0].enabled is False
    assert migrated.settings.to_dict() == settings_payload
    assert migrated.runtime.active == state_payload["ringing"]
    assert migrated.runtime.snoozes == [
        {"alarm_id": "7", "due_at": "2026-07-17T07:40:00"}
    ]
    assert next(repository.backup_dir.glob("*alarms.json*")).read_bytes() == alarm_bytes

    operational_bytes = repository.operational_file.read_bytes()
    repository.legacy_alarms_file.write_text(json.dumps([_legacy_alarm(enabled=True)]))
    second = repository.load()
    assert second.alarms[0].enabled is False
    assert second.revision == migrated.revision
    assert repository.operational_file.read_bytes() == operational_bytes


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"{not-json", "malformed_json"),
        (b"[]", "document_not_object"),
        (json.dumps({"schema_version": 2}).encode(), "unsupported_schema"),
        (json.dumps({"schema_version": 999}).encode(), "unsupported_schema"),
    ],
)
def test_invalid_operational_data_is_quarantined_and_never_defaulted(
    tmp_path, payload, code
):
    repository = _repo(tmp_path)
    repository.config_dir.mkdir(parents=True, exist_ok=True)
    repository.operational_file.write_bytes(payload)

    with pytest.raises(RepositoryError) as caught:
        repository.load()

    assert caught.value.code == code
    assert repository.operational_file.read_bytes() == payload
    evidence = list(repository.quarantine_dir.glob("*.evidence"))
    metadata = list(repository.quarantine_dir.glob("*.metadata.json"))
    assert len(evidence) == len(metadata) == 1
    assert evidence[0].read_bytes() == payload
    assert json.loads(metadata[0].read_text())["code"] == code
    assert payload.decode(errors="ignore") not in str(caught.value)


def test_invalid_legacy_source_fails_closed_without_operational_defaults(tmp_path):
    repository = _repo(tmp_path)
    repository.config_dir.mkdir(parents=True, exist_ok=True)
    secret = b'{"web_pin":"do-not-print", broken}'
    repository.legacy_settings_file.write_bytes(secret)

    with pytest.raises(MigrationError) as caught:
        repository.load()

    assert not repository.operational_file.exists()
    assert "do-not-print" not in str(caught.value)
    metadata_text = next(repository.quarantine_dir.glob("*.metadata.json")).read_text()
    assert "do-not-print" not in metadata_text
    assert next(repository.quarantine_dir.glob("*.evidence")).read_bytes() == secret


def test_unreadable_source_is_preserved_and_reported_without_contents(tmp_path):
    repository = _repo(tmp_path)
    repository.config_dir.mkdir(parents=True, exist_ok=True)
    secret = b'[{"label":"private-alarm"}]'
    repository.legacy_alarms_file.write_bytes(secret)
    repository.legacy_alarms_file.chmod(0)

    try:
        with pytest.raises(QuarantineError) as caught:
            repository.load()
    finally:
        repository.legacy_alarms_file.chmod(0o600)

    assert caught.value.code == "unreadable_source"
    assert "private-alarm" not in str(caught.value)
    assert next(repository.quarantine_dir.glob("*.evidence")).read_bytes() == secret
    assert not repository.operational_file.exists()


def test_backup_and_quarantine_collisions_never_overwrite_evidence(tmp_path):
    repository = _repo(tmp_path)
    repository.config_dir.mkdir(parents=True, exist_ok=True)
    repository.legacy_alarms_file.write_bytes(b"[]")
    repository.load()
    first_backup = next(repository.backup_dir.iterdir())
    repository._backup_bytes(repository.legacy_alarms_file, b"second")
    backups = sorted(repository.backup_dir.iterdir())
    assert len(backups) == 2
    assert first_backup.read_bytes() == b"[]"
    assert {item.read_bytes() for item in backups} == {b"[]", b"second"}

    repository._quarantine_bytes(repository.operational_file, b"first", "bad")
    repository._quarantine_bytes(repository.operational_file, b"second", "bad")
    evidence = sorted(repository.quarantine_dir.glob("*.evidence"))
    assert len(evidence) == 2
    assert {item.read_bytes() for item in evidence} == {b"first", b"second"}


def test_operational_document_rejects_invalid_nested_runtime_before_construction():
    payload = {
        "schema_version": 1,
        "revision": 0,
        "alarms": [],
        "settings": {
            "audio_player": "mpv",
            "audio_player_args": [],
            "default_sound": "/usr/share/sounds/alsa/Front_Center.wav",
            "default_snooze_minutes": 5,
            "check_interval_seconds": 5,
            "max_volume": 100,
            "web_enabled": False,
            "web_host": "127.0.0.1",
            "web_port": 8765,
            "web_pin": None,
        },
        "runtime": {
            "scheduler_checkpoint": None,
            "active": None,
            "queue": "not-a-list",
            "snoozes": [],
            "accepted_occurrences": [],
            "diagnostics": [],
        },
    }
    with pytest.raises(ValueError):
        OperationalDocument.from_payload(payload)


def test_config_exposes_one_default_operational_repository():
    repository = config.get_repository()

    assert repository is config.get_repository()
    assert repository.operational_file == config.OPERATIONAL_FILE
    assert repository.lock_file == config.OPERATIONAL_LOCK_FILE
    assert repository.backup_dir == config.BACKUP_DIR
    assert repository.quarantine_dir == config.QUARANTINE_DIR
