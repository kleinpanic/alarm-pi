import json

from alarm import config
from alarm.models import Alarm


def _mk(id="1", time="07:00"):
    return Alarm(id=id, label=f"a{id}", time=time, days_of_week=[0, 1, 2])


def test_add_get_update_delete():
    config.add_alarm(_mk("1"))
    config.add_alarm(_mk("2", "08:00"))
    assert {a.id for a in config.load_alarms()} == {"1", "2"}

    a = config.get_alarm_by_id("1")
    a.time = "09:15"
    assert config.update_alarm(a) is True
    assert config.get_alarm_by_id("1").time == "09:15"

    assert config.delete_alarm("2") is True
    assert config.delete_alarm("nope") is False
    assert {a.id for a in config.load_alarms()} == {"1"}


def test_generate_alarm_id_skips_existing():
    config.add_alarm(_mk("1"))
    config.add_alarm(_mk("3"))
    assert config.generate_alarm_id() == "2"


def test_atomic_write_produces_valid_json_and_no_tmp():
    config.add_alarm(_mk("1"))
    data = json.loads(config.ALARMS_FILE.read_text())
    assert isinstance(data, list) and data[0]["id"] == "1"
    leftovers = list(config.CONFIG_DIR.glob(".alarms.json.*.tmp"))
    assert leftovers == []


def test_malformed_alarms_returns_empty():
    config.ensure_config_dir()
    config.ALARMS_FILE.write_text("{ not json")
    assert config.load_alarms() == []
