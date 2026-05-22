"""Shared test fixtures. Points ALARM_PROJECT_ROOT at a temp dir before the
alarm package is imported, and resets config files between tests."""

import os
import tempfile

# Must be set before any `alarm.*` import so config paths resolve to the sandbox.
_TMP = tempfile.mkdtemp(prefix="alarm-tests-")
os.environ["ALARM_PROJECT_ROOT"] = _TMP

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_config():
    """Wipe alarms/settings/state before each test."""
    from alarm import config
    config.ensure_config_dir()
    for f in (config.ALARMS_FILE, config.SETTINGS_FILE, config.STATE_FILE):
        if f.exists():
            f.unlink()
    yield
