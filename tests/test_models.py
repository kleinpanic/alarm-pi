from alarm.models import Alarm, Settings


def test_alarm_roundtrip():
    a = Alarm(id="1", label="Wake", time="07:00", days_of_week=[0, 1, 2, 3, 4],
              irritable=True, base_volume=80)
    assert Alarm.from_dict(a.to_dict()) == a


def test_alarm_defaults_from_partial_dict():
    a = Alarm.from_dict({"id": 5, "time": "08:30"})
    assert a.id == "5"  # coerced to str
    assert a.label == "Unnamed Alarm"
    assert a.days_of_week == [0, 1, 2, 3, 4]
    assert a.enabled is True


def test_settings_web_defaults():
    s = Settings()
    assert s.web_enabled is True
    assert s.web_host == "0.0.0.0"
    assert s.web_port == 8765
    assert s.web_pin is None


def test_settings_roundtrip_with_pin():
    s = Settings(web_port=9000, web_pin="1234")
    s2 = Settings.from_dict(s.to_dict())
    assert s2.web_port == 9000
    assert s2.web_pin == "1234"


def test_settings_empty_pin_becomes_none():
    assert Settings.from_dict({"web_pin": ""}).web_pin is None
