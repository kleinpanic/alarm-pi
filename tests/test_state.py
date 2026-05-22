from datetime import datetime, timedelta

from alarm.models import Alarm
from alarm.state import RuntimeState


def _mk(id="1"):
    return Alarm(id=id, label="wake", time="07:00", days_of_week=[0], snoozable=True)


def test_ringing_set_clear():
    s = RuntimeState()
    assert s.get_ringing() is None
    s.set_ringing(_mk("7"))
    r = s.get_ringing()
    assert r.alarm_id == "7" and r.label == "wake"
    s.clear_ringing()
    assert s.get_ringing() is None


def test_snooze_due_and_cancel():
    s = RuntimeState()
    past = datetime.now() - timedelta(minutes=1)
    future = datetime.now() + timedelta(minutes=10)
    s.set_snooze("1", past)
    s.set_snooze("2", future)
    assert s.pop_due_snoozes() == ["1"]
    assert s.pop_due_snoozes() == []  # consumed
    s.cancel_snooze("2")
    assert s.snoozes() == {}


def test_persistence_reload():
    s = RuntimeState()
    s.set_ringing(_mk("9"))
    s.set_snooze("3", datetime.now() + timedelta(minutes=5))
    s2 = RuntimeState()  # reloads from state.json
    assert s2.get_ringing().alarm_id == "9"
    assert "3" in s2.snoozes()
