"""Versioned, fail-closed operational persistence and legacy migration."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Callable, Mapping, Optional

from .models import Alarm, OperationalDocument, RuntimeData, Settings


SCHEMA_VERSION = 1


class RepositoryError(RuntimeError):
    """A safe startup error with recovery metadata but no source contents."""

    def __init__(
        self,
        code: str,
        source: Path,
        *,
        evidence_path: Optional[Path] = None,
        metadata_path: Optional[Path] = None,
    ) -> None:
        self.code = code
        self.source = source
        self.evidence_path = evidence_path
        self.metadata_path = metadata_path
        message = f"Operational data rejected ({code}) at {source.name}"
        if evidence_path is not None:
            message += f"; recovery evidence: {evidence_path.name}"
        super().__init__(message)


class MigrationError(RepositoryError):
    """A legacy source could not be migrated safely."""


class QuarantineError(MigrationError):
    """A source was unreadable or could not be safely quarantined."""


class OperationalRepository:
    """Own the schema-v1 document, migration evidence, and durable replacement."""

    def __init__(
        self,
        config_dir: Path | str,
        *,
        timestamp: Optional[Callable[[], str]] = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.operational_file = self.config_dir / "operational.json"
        self.lock_file = self.config_dir / "operational.lock"
        self.backup_dir = self.config_dir / "backups"
        self.quarantine_dir = self.config_dir / "quarantine"
        self.legacy_alarms_file = self.config_dir / "alarms.json"
        self.legacy_settings_file = self.config_dir / "settings.json"
        self.legacy_state_file = self.config_dir / "state.json"
        self._timestamp = timestamp or (
            lambda: datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        )

    def load(self) -> OperationalDocument:
        """Load, bootstrap, or migrate while never treating invalid data as absence."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            self._cleanup_stale_temps_locked()
            return self._detached(self._load_or_bootstrap_locked())

    def snapshot(self) -> OperationalDocument:
        """Return a detached, validated point-in-time document."""
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Cleanup is a mutation of repository-owned artifacts and therefore runs
        # under an exclusive lock before the ordinary shared read lock.
        with self._exclusive_lock():
            self._cleanup_stale_temps_locked()
            if not self.operational_file.exists():
                return self._detached(self._load_or_bootstrap_locked())
        with self._lock(fcntl.LOCK_SH):
            return self._detached(self._load_operational())

    def transaction(self, mutation: Callable[[OperationalDocument], Any]) -> Any:
        """Run one complete read/modify/validate/write operation exclusively.

        The aggregate revision advances exactly once when serialized content
        changes and is stable for idempotent commands.
        """
        self.config_dir.mkdir(parents=True, exist_ok=True)
        with self._exclusive_lock():
            self._cleanup_stale_temps_locked()
            current = self._load_or_bootstrap_locked()
            candidate = self._detached(current)
            result = mutation(candidate)
            validated = OperationalDocument.from_payload(candidate.to_dict())
            before = current.to_dict()
            after = validated.to_dict()
            before.pop("revision")
            after.pop("revision")
            if after != before:
                validated.revision = current.revision + 1
                self._atomic_commit(validated)
            return self._detached_value(result)

    def create_alarm(self, alarm: Alarm) -> Alarm:
        """Create with the lowest free positive decimal ID under the lock."""
        created: Optional[Alarm] = None

        def create(document: OperationalDocument) -> None:
            nonlocal created
            used = {item.id for item in document.alarms}
            number = 1
            while str(number) in used:
                number += 1
            payload = alarm.to_dict()
            payload["id"] = str(number)
            created = Alarm.from_payload(payload)
            document.alarms.append(created)

        self.transaction(create)
        assert created is not None
        return Alarm.from_payload(created.to_dict())

    def update_alarm(self, alarm: Alarm) -> bool:
        updated = False

        def update(document: OperationalDocument) -> None:
            nonlocal updated
            replacement = Alarm.from_payload(alarm.to_dict())
            for index, existing in enumerate(document.alarms):
                if existing.id == replacement.id:
                    updated = True
                    document.alarms[index] = replacement
                    return

        self.transaction(update)
        return updated

    def delete_alarm(self, alarm_id: str) -> bool:
        """Delete an alarm and its pending/queued snooze work atomically."""
        deleted = False

        def delete(document: OperationalDocument) -> None:
            nonlocal deleted
            kept = [alarm for alarm in document.alarms if alarm.id != alarm_id]
            deleted = len(kept) != len(document.alarms)
            document.alarms = kept
            if deleted:
                self._cancel_snooze_work(document.runtime, alarm_id)

        self.transaction(delete)
        return deleted

    def disable_alarm(self, alarm_id: str) -> bool:
        """Disable an alarm and cancel its pending/queued snooze work atomically."""
        changed = False

        def disable(document: OperationalDocument) -> None:
            nonlocal changed
            for alarm in document.alarms:
                if alarm.id == alarm_id:
                    changed = alarm.enabled
                    alarm.enabled = False
                    self._cancel_snooze_work(document.runtime, alarm_id)
                    return

        self.transaction(disable)
        return changed

    @staticmethod
    def _cancel_snooze_work(runtime: RuntimeData, alarm_id: str) -> None:
        runtime.snoozes = [
            item for item in runtime.snoozes if item.get("alarm_id") != alarm_id
        ]
        runtime.queue = [
            item
            for item in runtime.queue
            if not (item.get("alarm_id") == alarm_id and item.get("kind") == "snooze")
        ]

    def replace_alarms(self, alarms: list[Alarm]) -> None:
        replacements = [Alarm.from_payload(alarm.to_dict()) for alarm in alarms]
        self.transaction(lambda document: setattr(document, "alarms", replacements))

    def set_settings(self, settings: Settings) -> None:
        replacement = Settings.from_payload(settings.to_dict())
        self.transaction(lambda document: setattr(document, "settings", replacement))

    def set_runtime(self, runtime: RuntimeData) -> None:
        replacement = RuntimeData.from_payload(runtime.to_dict())
        self.transaction(lambda document: setattr(document, "runtime", replacement))

    def _load_or_bootstrap_locked(self) -> OperationalDocument:
        if self.operational_file.exists():
            return self._load_operational()
        document, sources = self._build_from_legacy()
        for source, raw in sources:
            self._backup_bytes(source, raw)
        self._atomic_commit(document)
        return document

    @staticmethod
    def _detached(document: OperationalDocument) -> OperationalDocument:
        return OperationalDocument.from_payload(document.to_dict())

    @staticmethod
    def _detached_value(value: Any) -> Any:
        if isinstance(value, Alarm):
            return Alarm.from_payload(value.to_dict())
        if isinstance(value, Settings):
            return Settings.from_payload(value.to_dict())
        if isinstance(value, RuntimeData):
            return RuntimeData.from_payload(value.to_dict())
        return value

    @contextlib.contextmanager
    def _exclusive_lock(self):
        with self._lock(fcntl.LOCK_EX):
            yield

    @contextlib.contextmanager
    def _lock(self, operation: int):
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a+") as lock:
            fcntl.flock(lock.fileno(), operation)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _cleanup_stale_temps_locked(self) -> None:
        """Remove only regular files created by this repository's writer."""
        pattern = re.compile(r"^\.operational\.json\.[A-Za-z0-9_-]+\.tmp$")
        for entry in self.config_dir.iterdir():
            if not pattern.fullmatch(entry.name):
                continue
            try:
                mode = entry.lstat().st_mode
                if stat.S_ISREG(mode):
                    entry.unlink()
            except FileNotFoundError:
                pass

    def _load_operational(self) -> OperationalDocument:
        raw = self._read_bytes(self.operational_file, migration=False)
        data = self._parse_json(self.operational_file, raw, migration=False)
        if not isinstance(data, Mapping):
            self._raise_quarantined(
                RepositoryError, self.operational_file, raw, "document_not_object"
            )
        version = data.get("schema_version")
        if type(version) is not int or version != SCHEMA_VERSION:
            self._raise_quarantined(
                RepositoryError, self.operational_file, raw, "unsupported_schema"
            )
        try:
            return OperationalDocument.from_payload(data)
        except (TypeError, ValueError):
            self._raise_quarantined(
                RepositoryError, self.operational_file, raw, "invalid_document"
            )

    def _build_from_legacy(
        self,
    ) -> tuple[OperationalDocument, list[tuple[Path, bytes]]]:
        sources: list[tuple[Path, bytes]] = []

        alarms: list[Alarm] = []
        if self.legacy_alarms_file.exists():
            raw = self._read_bytes(self.legacy_alarms_file, migration=True)
            sources.append((self.legacy_alarms_file, raw))
            data = self._parse_json(self.legacy_alarms_file, raw, migration=True)
            if type(data) is not list:
                self._raise_quarantined(
                    MigrationError, self.legacy_alarms_file, raw, "legacy_alarms_not_list"
                )
            try:
                alarms = [self._migrate_alarm(item) for item in data]
            except (TypeError, ValueError):
                self._raise_quarantined(
                    MigrationError, self.legacy_alarms_file, raw, "invalid_legacy_alarms"
                )

        settings = Settings()
        if self.legacy_settings_file.exists():
            raw = self._read_bytes(self.legacy_settings_file, migration=True)
            sources.append((self.legacy_settings_file, raw))
            data = self._parse_json(self.legacy_settings_file, raw, migration=True)
            try:
                settings = self._migrate_settings(data)
            except (TypeError, ValueError):
                self._raise_quarantined(
                    MigrationError, self.legacy_settings_file, raw, "invalid_legacy_settings"
                )

        runtime = RuntimeData()
        if self.legacy_state_file.exists():
            raw = self._read_bytes(self.legacy_state_file, migration=True)
            sources.append((self.legacy_state_file, raw))
            data = self._parse_json(self.legacy_state_file, raw, migration=True)
            try:
                runtime = self._migrate_runtime(data)
            except (TypeError, ValueError):
                self._raise_quarantined(
                    MigrationError, self.legacy_state_file, raw, "invalid_legacy_state"
                )

        try:
            document = OperationalDocument.from_payload(
                {
                    "schema_version": SCHEMA_VERSION,
                    "revision": 0,
                    "alarms": [alarm.to_dict() for alarm in alarms],
                    "settings": settings.to_dict(),
                    "runtime": runtime.to_dict(),
                }
            )
        except (TypeError, ValueError) as exc:
            # The individual source adapters validate first, so reaching this point
            # indicates an internal migration contract failure rather than absence.
            raise MigrationError("invalid_migration_candidate", self.config_dir) from exc
        return document, sources

    @staticmethod
    def _migrate_alarm(value: Any) -> Alarm:
        if not isinstance(value, Mapping):
            raise ValueError("legacy alarm must be an object")
        allowed = set(Alarm.__dataclass_fields__)
        if any(key not in allowed for key in value):
            raise ValueError("legacy alarm contains unknown fields")
        return Alarm.from_payload(Alarm.from_dict(dict(value)).to_dict())

    @staticmethod
    def _migrate_settings(value: Any) -> Settings:
        if not isinstance(value, Mapping):
            raise ValueError("legacy settings must be an object")
        allowed = set(Settings.__dataclass_fields__)
        if any(key not in allowed for key in value):
            raise ValueError("legacy settings contains unknown fields")
        return Settings.from_payload(Settings.from_dict(dict(value)).to_dict())

    @staticmethod
    def _migrate_runtime(value: Any) -> RuntimeData:
        if not isinstance(value, Mapping) or any(
            key not in {"ringing", "snoozes"} for key in value
        ):
            raise ValueError("legacy state must be a recognized object")
        active = value.get("ringing")
        if active is not None and not isinstance(active, Mapping):
            raise ValueError("legacy ringing state must be an object or null")
        snoozes = value.get("snoozes") or {}
        if not isinstance(snoozes, Mapping):
            raise ValueError("legacy snoozes must be an object")
        migrated_snoozes = []
        for alarm_id, due_at in sorted(snoozes.items(), key=lambda item: str(item[0])):
            if type(due_at) is not str:
                raise ValueError("legacy snooze timestamp must be a string")
            migrated_snoozes.append({"alarm_id": str(alarm_id), "due_at": due_at})
        return RuntimeData.from_payload(
            {
                "scheduler_checkpoint": None,
                "active": None if active is None else dict(active),
                "queue": [],
                "snoozes": migrated_snoozes,
                "accepted_occurrences": [],
                "diagnostics": [],
            }
        )

    def _read_bytes(self, source: Path, *, migration: bool) -> bytes:
        try:
            mode = source.stat().st_mode
            raw = source.read_bytes()
        except OSError as exc:
            error_type = QuarantineError if migration else RepositoryError
            raw = self._recover_unreadable_bytes(source)
            evidence, metadata = self._quarantine_bytes(source, raw, "unreadable_source")
            raise error_type(
                "unreadable_source", source, evidence_path=evidence, metadata_path=metadata
            ) from exc
        if stat.S_IMODE(mode) & 0o444 == 0:
            error_type = QuarantineError if migration else RepositoryError
            evidence, metadata = self._quarantine_bytes(source, raw, "unreadable_source")
            raise error_type(
                "unreadable_source", source, evidence_path=evidence, metadata_path=metadata
            )
        return raw

    @staticmethod
    def _recover_unreadable_bytes(source: Path) -> bytes:
        """Best-effort evidence copy for an owned file with no read permission bits."""
        try:
            original_mode = stat.S_IMODE(source.stat().st_mode)
            source.chmod(original_mode | stat.S_IRUSR)
            try:
                return source.read_bytes()
            finally:
                source.chmod(original_mode)
        except OSError:
            return b""

    def _parse_json(self, source: Path, raw: bytes, *, migration: bool) -> Any:
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            error_type = MigrationError if migration else RepositoryError
            self._raise_quarantined(error_type, source, raw, "malformed_json")

    def _raise_quarantined(self, error_type, source: Path, raw: bytes, code: str):
        evidence, metadata = self._quarantine_bytes(source, raw, code)
        raise error_type(code, source, evidence_path=evidence, metadata_path=metadata)

    def _backup_bytes(self, source: Path, raw: bytes) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        path = self._unique_path(self.backup_dir, f"{self._timestamp()}--{source.name}")
        self._write_private(path, raw)
        return path

    def _quarantine_bytes(self, source: Path, raw: bytes, code: str) -> tuple[Path, Path]:
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{self._timestamp()}--{source.name}"
        suffix = 0
        while True:
            candidate = stem if suffix == 0 else f"{stem}.{suffix}"
            evidence = self.quarantine_dir / f"{candidate}.evidence"
            metadata = self.quarantine_dir / f"{candidate}.metadata.json"
            if not evidence.exists() and not metadata.exists():
                break
            suffix += 1
        self._write_private(evidence, raw)
        metadata_payload = {
            "code": code,
            "source": source.name,
            "timestamp": self._timestamp(),
            "evidence": evidence.name,
            "remediation": "Inspect preserved evidence and restore or migrate valid data.",
        }
        self._write_private(
            metadata,
            (json.dumps(metadata_payload, sort_keys=True, indent=2) + "\n").encode(),
        )
        return evidence, metadata

    @staticmethod
    def _unique_path(directory: Path, basename: str) -> Path:
        candidate = directory / basename
        suffix = 0
        while candidate.exists():
            suffix += 1
            candidate = directory / f"{basename}.{suffix}"
        return candidate

    @staticmethod
    def _write_private(path: Path, raw: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    def _atomic_commit(self, document: OperationalDocument) -> None:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=self.config_dir,
            prefix=f".{self.operational_file.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document.to_dict(), handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._before_replace(Path(temp_name))
            os.replace(temp_name, self.operational_file)
            directory_fd = os.open(self.config_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_name)
            raise

    def _before_replace(self, temp_path: Path) -> None:
        """Test seam after durable temp write and before atomic replacement."""


__all__ = [
    "OperationalRepository",
    "RepositoryError",
    "MigrationError",
    "QuarantineError",
]
