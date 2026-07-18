"""Shared test fixtures. Points ALARM_PROJECT_ROOT at a temp dir before the
alarm package is imported, and resets config files between tests."""

import os
import shutil
import tempfile

# Must be set before any `alarm.*` import so config paths resolve to the sandbox.
_TMP = tempfile.mkdtemp(prefix="alarm-tests-")
os.environ["ALARM_PROJECT_ROOT"] = _TMP

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_config():
    """Wipe every legacy and operational persistence artifact before each test."""
    from alarm import config
    config.ensure_config_dir()
    for f in (
        config.ALARMS_FILE,
        config.SETTINGS_FILE,
        config.STATE_FILE,
        getattr(config, "OPERATIONAL_FILE", config.CONFIG_DIR / "operational.json"),
        getattr(config, "OPERATIONAL_LOCK_FILE", config.CONFIG_DIR / "operational.lock"),
    ):
        if f.exists():
            f.unlink()
    for directory in (
        getattr(config, "BACKUP_DIR", config.CONFIG_DIR / "backups"),
        getattr(config, "QUARANTINE_DIR", config.CONFIG_DIR / "quarantine"),
    ):
        if directory.exists():
            shutil.rmtree(directory)
    yield
