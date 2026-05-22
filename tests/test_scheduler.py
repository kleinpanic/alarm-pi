from datetime import datetime, timedelta

from alarm.models import Alarm
from alarm.scheduler import AlarmScheduler
from alarm.state import RuntimeState


def _mk(id="1", time="07:00", days=(0, 1, 2, 3, 4), enabled=True, skip=None):
    return Alarm(id=id, label="wake", time=time, days_of_week=list(days),
                 enabled=enabled, skip_dates=list(skip or []))


# A known Monday 07:00
MON = datetime(2026, 5, 18, 7, 0, 0)


def test_fires_on_matching_minute():
    sch = AlarmScheduler()
    fired = sch.check_alarms([_mk("1")], now=MON)
    assert [a.id for a in fired] == ["1"]


def test_no_double_fire_same_minute():
    sch = AlarmScheduler()
    assert sch.check_alarms([_mk("1")], now=MON)
    assert sch.check_alarms([_mk("1")], now=MON) == []


def test_disabled_and_wrong_day_and_skip():
    sch = AlarmScheduler()
    assert sch.check_alarms([_mk("1", enabled=False)], now=MON) == []
    assert sch.check_alarms([_mk("2", days=(5, 6))], now=MON) == []  # Mon not in weekend
    assert sch.check_alarms([_mk("3", skip=["2026-05-18"])], now=MON) == []


def test_snooze_due_fires_via_state():
    state = RuntimeState()
    sch = AlarmScheduler(state=state)
    alarm = _mk("1", time="23:59")  # won't match MON time
    sch.schedule_snooze(alarm, snooze_minutes=5, now=MON)
    # snooze due 5 min later
    fired = sch.check_alarms([alarm], now=MON + timedelta(minutes=6))
    assert [a.id for a in fired] == ["1"]


def test_get_next_alarms_sorted():
    a1 = _mk("1", time="07:00", days=(0, 1, 2, 3, 4))
    a2 = _mk("2", time="06:00", days=(0, 1, 2, 3, 4))
    sch = AlarmScheduler()
    nxt = sch.get_next_alarms([a1, a2], count=2, now=datetime(2026, 5, 18, 5, 0))
    assert [a.id for a, _ in nxt] == ["2", "1"]  # 06:00 before 07:00
