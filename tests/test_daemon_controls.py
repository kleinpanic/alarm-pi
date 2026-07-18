from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from alarm.daemon_core import AlarmDaemon, SNOOZE_MAX_MINUTES, SNOOZE_MIN_MINUTES
from alarm.models import Alarm, AlarmSnapshot, Occurrence, SchedulerDiagnostic
from alarm.repository import OperationalRepository


UTC = ZoneInfo("UTC")
NOW = datetime(2026, 7, 17, 8, 0, tzinfo=UTC)


def _alarm(alarm_id="1", *, enabled=True, snoozable=True):
    return Alarm(
        id=alarm_id,
        label=f"alarm-{alarm_id}",
        time="08:00",
        days_of_week=[4],
        enabled=enabled,
        snoozable=snoozable,
    )


def _occurrence(alarm, occurrence_id, *, kind="scheduled", due_at=NOW):
    return Occurrence(
        occurrence_id=occurrence_id,
        alarm_id=alarm.id,
        kind=kind,
        due_at=due_at,
        accepted_at=None,
        alarm=AlarmSnapshot.from_alarm(alarm),
    )


class FakeAudio:
    def __init__(self, repository):
        self.repository = repository
        self.played = []
        self.stop_calls = 0
        self.settings = None

    def play(self, alarm):
        active = self.repository.snapshot().runtime.active
        assert active is not None and active["alarm_id"] == alarm.id
        self.played.append(alarm.id)
        return True

    def stop(self):
        self.stop_calls += 1


def _daemon(tmp_path, alarms, *, now=NOW):
    repository = OperationalRepository(tmp_path)
    repository.load()
    repository.replace_alarms(alarms)
    audio = FakeAudio(repository)
    daemon = AlarmDaemon(repository=repository, audio=audio, clock=lambda: now, timezone=UTC)

    def fake_popup(alarm):
        runtime = repository.snapshot().runtime
        assert runtime.active["alarm_id"] == alarm.id
        assert runtime.scheduler_checkpoint == now.isoformat()
        assert runtime.active["occurrence_id"] in runtime.accepted_occurrences

    daemon._spawn_popup = fake_popup
    daemon._kill_popup = lambda: None
    return daemon, repository, audio


def test_runtime_only_write_does_not_reload_configuration(tmp_path, monkeypatch):
    daemon, repository, _ = _daemon(tmp_path, [_alarm()])
    loaded_alarms = daemon.alarms
    daemon._config_mtime = (1.0, 1.0)
    monkeypatch.setattr("alarm.daemon_core.get_config_mtime", lambda: (2.0, 2.0))
    repository.transaction(
        lambda document: setattr(document.runtime, "scheduler_checkpoint", NOW.isoformat())
    )

    daemon._check_config_reload()

    assert daemon._config_mtime == (2.0, 2.0)
    assert daemon.alarms is loaded_alarms


def test_alarm_change_still_reloads_configuration(tmp_path, monkeypatch):
    daemon, repository, audio = _daemon(tmp_path, [_alarm("1")])
    daemon._config_mtime = (1.0, 1.0)
    monkeypatch.setattr("alarm.daemon_core.get_config_mtime", lambda: (2.0, 2.0))
    monkeypatch.setattr(daemon, "_start_web", lambda: None)
    repository.replace_alarms([_alarm("2")])

    daemon._check_config_reload()

    assert [alarm.id for alarm in daemon.alarms] == ["2"]
    assert audio.settings == daemon.settings


@pytest.mark.parametrize(
    "minutes, accepted",
    [
        (SNOOZE_MIN_MINUTES, True),
        (SNOOZE_MAX_MINUTES, True),
        (SNOOZE_MIN_MINUTES - 1, False),
        (SNOOZE_MAX_MINUTES + 1, False),
        (-1, False),
        (True, False),
        (1.5, False),
        ("5", False),
    ],
)
def test_snooze_strict_bounds_and_types_preserve_active_on_refusal(tmp_path, minutes, accepted):
    alarm = _alarm()
    daemon, repository, audio = _daemon(tmp_path, [alarm])
    daemon.admit_occurrences([_occurrence(alarm, "scheduled:1:slot")], (), NOW)

    assert daemon.snooze(minutes) is accepted
    runtime = repository.snapshot().runtime
    if accepted:
        assert runtime.active is None
        assert len(runtime.snoozes) == 1
        assert audio.stop_calls == 1
    else:
        assert runtime.active["occurrence_id"] == "scheduled:1:slot"
        assert runtime.snoozes == []
        assert audio.stop_calls == 0


def test_non_snoozable_alarm_is_refused_without_effects(tmp_path):
    alarm = _alarm(snoozable=False)
    daemon, repository, audio = _daemon(tmp_path, [alarm])
    daemon.admit_occurrences([_occurrence(alarm, "scheduled:1:slot")], (), NOW)

    assert daemon.snooze(5) is False
    assert repository.snapshot().runtime.active["occurrence_id"] == "scheduled:1:slot"
    assert audio.stop_calls == 0


@pytest.mark.parametrize("delete", [False, True])
def test_disable_or_delete_atomically_cancels_only_matching_snooze_work(tmp_path, delete):
    first, second = _alarm("1"), _alarm("2")
    daemon, repository, _ = _daemon(tmp_path, [first, second])
    repository.transaction(
        lambda document: (
            document.runtime.snoozes.extend(
                [
                    daemon._occurrence_record(_occurrence(first, "snooze:1:a", kind="snooze"), NOW),
                    daemon._occurrence_record(_occurrence(first, "snooze:1:b", kind="snooze"), NOW),
                    daemon._occurrence_record(_occurrence(second, "snooze:2:a", kind="snooze"), NOW),
                ]
            ),
            document.runtime.queue.extend(
                [
                    daemon._occurrence_record(_occurrence(first, "snooze:1:q", kind="snooze"), NOW),
                    daemon._occurrence_record(_occurrence(second, "snooze:2:q", kind="snooze"), NOW),
                ]
            ),
        )
    )
    before = repository.snapshot().revision

    changed = daemon.delete_alarm("1") if delete else daemon.disable_alarm("1")

    snapshot = repository.snapshot()
    assert changed is True and snapshot.revision == before + 1
    assert {item["alarm_id"] for item in snapshot.runtime.snoozes} == {"2"}
    assert {item["alarm_id"] for item in snapshot.runtime.queue} == {"2"}
    remaining = {alarm.id: alarm for alarm in snapshot.alarms}
    assert ("1" not in remaining) if delete else (remaining["1"].enabled is False)


def test_collision_queue_order_persists_and_dismiss_promotes(tmp_path):
    first, second = _alarm("1"), _alarm("2")
    daemon, repository, audio = _daemon(tmp_path, [first, second])
    candidates = [
        _occurrence(second, "snooze:2:a", kind="snooze"),
        _occurrence(second, "scheduled:2:slot"),
        _occurrence(first, "scheduled:1:slot"),
    ]

    assert daemon.admit_occurrences(candidates, (), NOW) == 3
    runtime = repository.snapshot().runtime
    assert runtime.active["occurrence_id"] == "scheduled:1:slot"
    assert [item["occurrence_id"] for item in runtime.queue] == [
        "scheduled:2:slot",
        "snooze:2:a",
    ]
    assert daemon.admit_occurrences(candidates, (), NOW) == 0

    assert daemon.dismiss() is True
    assert repository.snapshot().runtime.active["occurrence_id"] == "scheduled:2:slot"
    assert audio.played == ["1", "2"]


def test_due_snooze_is_reauthorized_and_ineligible_source_is_diagnostic(tmp_path):
    eligible, disabled, blocked = _alarm("1"), _alarm("2", enabled=False), _alarm("3", snoozable=False)
    daemon, repository, _ = _daemon(tmp_path, [eligible, disabled, blocked])
    candidates = [
        _occurrence(eligible, "snooze:1:a", kind="snooze"),
        _occurrence(disabled, "snooze:2:a", kind="snooze"),
        _occurrence(blocked, "snooze:3:a", kind="snooze"),
        _occurrence(_alarm("missing"), "snooze:missing:a", kind="snooze"),
    ]

    assert daemon.admit_occurrences(candidates, (), NOW) == 1
    runtime = repository.snapshot().runtime
    assert runtime.active["occurrence_id"] == "snooze:1:a"
    assert {item["code"] for item in runtime.diagnostics} == {
        "snooze_source_disabled",
        "snooze_source_missing",
        "snooze_source_not_snoozable",
    }


def test_side_effect_failure_does_not_roll_back_durable_admission(tmp_path):
    alarm = _alarm()
    daemon, repository, audio = _daemon(tmp_path, [alarm])

    def fail_after_commit(_alarm):
        assert repository.snapshot().runtime.active["occurrence_id"] == "scheduled:1:slot"
        raise RuntimeError("fake audio failure")

    audio.play = fail_after_commit
    with pytest.raises(RuntimeError, match="fake audio failure"):
        daemon.admit_occurrences([_occurrence(alarm, "scheduled:1:slot")], (), NOW)

    runtime = repository.snapshot().runtime
    assert runtime.active["occurrence_id"] == "scheduled:1:slot"
    assert runtime.accepted_occurrences == ["scheduled:1:slot"]


def test_restart_recovers_recent_once_and_classifies_stale_without_replay(tmp_path):
    first, second = _alarm("1"), _alarm("2")
    daemon, repository, _ = _daemon(tmp_path, [first, second])
    daemon.admit_occurrences(
        [
            _occurrence(first, "scheduled:1:slot", due_at=NOW - timedelta(minutes=1)),
            _occurrence(second, "scheduled:2:slot", due_at=NOW - timedelta(minutes=1)),
        ],
        (SchedulerDiagnostic(code="clock_backward", observed_at=NOW),),
        NOW,
    )
    runtime = repository.snapshot().runtime
    runtime.queue.append(
        daemon._occurrence_record(
            _occurrence(second, "scheduled:2:stale", due_at=NOW - timedelta(minutes=16)), NOW
        )
    )
    repository.set_runtime(runtime)

    audio = FakeAudio(repository)
    restarted = AlarmDaemon(repository=repository, audio=audio, clock=lambda: NOW, timezone=UTC)
    restarted._spawn_popup = lambda alarm: None
    assert restarted.recover() is True
    assert restarted.recover() is False

    recovered = repository.snapshot().runtime
    assert recovered.active["occurrence_id"] == "scheduled:1:slot"
    assert [item["occurrence_id"] for item in recovered.queue] == ["scheduled:2:slot"]
    assert "scheduled:2:stale" in {
        item.get("occurrence_id") for item in recovered.diagnostics if item["code"] == "recovery_stale"
    }
    assert audio.played == ["1"]
    assert restarted.admit_occurrences(
        [_occurrence(first, "scheduled:1:slot", due_at=NOW - timedelta(minutes=1))], (), NOW
    ) == 0


def test_restart_classifies_missing_disabled_and_non_snoozable_queue_entries(tmp_path):
    active = _alarm("1")
    disabled = _alarm("2", enabled=False)
    blocked = _alarm("3", snoozable=False)
    daemon, repository, _ = _daemon(tmp_path, [active, disabled, blocked])
    records = [
        daemon._occurrence_record(_occurrence(active, "scheduled:1:ok"), NOW),
        daemon._occurrence_record(_occurrence(disabled, "scheduled:2:disabled"), NOW),
        daemon._occurrence_record(_occurrence(blocked, "snooze:3:blocked", kind="snooze"), NOW),
        daemon._occurrence_record(
            _occurrence(_alarm("missing"), "scheduled:missing:gone"), NOW
        ),
    ]

    def seed(document):
        document.runtime.active = records[0]
        document.runtime.queue = records[1:]
        document.runtime.accepted_occurrences = [item["occurrence_id"] for item in records]

    repository.transaction(seed)
    restarted = AlarmDaemon(
        repository=repository,
        audio=FakeAudio(repository),
        clock=lambda: NOW,
        timezone=UTC,
    )
    restarted._spawn_popup = lambda alarm: None

    assert restarted.recover() is True
    runtime = repository.snapshot().runtime
    assert runtime.queue == []
    assert {item["code"] for item in runtime.diagnostics} == {
        "source_disabled",
        "source_missing",
        "snooze_source_not_snoozable",
    }
