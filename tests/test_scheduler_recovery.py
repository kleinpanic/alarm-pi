from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from alarm.models import Alarm, AlarmSnapshot, Occurrence
from alarm.scheduler import AlarmScheduler, CATCH_UP_MINUTES


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


def _alarm(
    alarm_id: str = "1",
    *,
    time: str = "07:00",
    days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6),
) -> Alarm:
    return Alarm(
        id=alarm_id,
        label=f"alarm-{alarm_id}",
        time=time,
        days_of_week=list(days),
    )


def test_first_evaluation_initializes_checkpoint_without_retroactive_scan():
    now = datetime(2026, 7, 17, 7, 0, tzinfo=NEW_YORK)

    result = AlarmScheduler().evaluate_interval(
        [_alarm(time="06:59"), _alarm("2", time="07:00")],
        checkpoint=None,
        now=now,
        timezone=NEW_YORK,
    )

    assert result.occurrences == ()
    assert result.diagnostics == ()
    assert result.checkpoint == now
    assert result.checkpoint.tzinfo is not None


def test_only_persisted_checkpoint_enables_following_catch_up():
    initialized = datetime(2026, 7, 17, 6, 59, 59, tzinfo=NEW_YORK)
    now = initialized + timedelta(seconds=1)

    result = AlarmScheduler().evaluate_interval(
        [_alarm()],
        checkpoint=initialized,
        now=now,
        timezone=NEW_YORK,
    )

    assert [item.occurrence_id for item in result.occurrences] == [
        "scheduled:1:2026-07-17:07:00"
    ]
    assert result.checkpoint == now


def test_interval_is_open_at_checkpoint_and_closed_at_now():
    scheduler = AlarmScheduler()
    at_seven = datetime(2026, 7, 17, 7, 0, tzinfo=NEW_YORK)

    excluded = scheduler.evaluate_interval(
        [_alarm()], at_seven, at_seven + timedelta(seconds=59), NEW_YORK
    )
    included = scheduler.evaluate_interval(
        [_alarm()], at_seven - timedelta(microseconds=1), at_seven, NEW_YORK
    )

    assert excluded.occurrences == ()
    assert [item.alarm_id for item in included.occurrences] == ["1"]


def test_empty_schedule_is_stable_and_advances_forward_checkpoint():
    checkpoint = datetime(2026, 7, 17, 6, 0, tzinfo=UTC)
    now = checkpoint + timedelta(minutes=2)

    result = AlarmScheduler().evaluate_interval([], checkpoint, now, UTC)

    assert result.occurrences == ()
    assert result.diagnostics == ()
    assert result.checkpoint == now


def test_exact_catch_up_horizon_is_admitted_and_older_slot_is_stale():
    now = datetime(2026, 7, 17, 7, 15, tzinfo=UTC)
    checkpoint = now - timedelta(minutes=CATCH_UP_MINUTES + 2)

    result = AlarmScheduler().evaluate_interval(
        [_alarm("old", time="06:59"), _alarm("edge", time="07:00")],
        checkpoint,
        now,
        UTC,
    )

    assert [item.alarm_id for item in result.occurrences] == ["edge"]
    assert [(item.code, item.alarm_id) for item in result.diagnostics] == [
        ("missed_stale", "old")
    ]


def test_accepted_ids_filter_repeated_evaluation():
    now = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    occurrence_id = "scheduled:1:2026-07-17:07:00"

    result = AlarmScheduler().evaluate_interval(
        [_alarm()],
        now - timedelta(minutes=1),
        now,
        UTC,
        accepted_occurrence_ids={occurrence_id},
    )

    assert result.occurrences == ()


def test_accepted_snooze_id_is_not_replayed():
    now = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    snooze = _snooze(_alarm(time="08:00"), now, "snooze:1:stable-id")

    result = AlarmScheduler().evaluate_interval(
        [],
        now - timedelta(minutes=1),
        now,
        UTC,
        accepted_occurrence_ids={snooze.occurrence_id},
        snoozes=[snooze],
    )

    assert result.occurrences == ()


def test_backward_clock_preserves_high_water_and_reports_diagnostic():
    checkpoint = datetime(2026, 7, 17, 7, 5, tzinfo=UTC)
    now = checkpoint - timedelta(minutes=5)

    result = AlarmScheduler().evaluate_interval([_alarm()], checkpoint, now, UTC)

    assert result.occurrences == ()
    assert result.checkpoint == checkpoint
    assert [item.code for item in result.diagnostics] == ["clock_backward"]


def test_spring_gap_is_skipped_without_shifting_local_slot():
    checkpoint = datetime(2026, 3, 8, 1, 55, tzinfo=NEW_YORK)
    now = datetime(2026, 3, 8, 3, 5, tzinfo=NEW_YORK)

    result = AlarmScheduler().evaluate_interval(
        [_alarm(time="02:30")], checkpoint, now, NEW_YORK
    )

    assert result.occurrences == ()
    assert [(item.code, item.scheduled_for) for item in result.diagnostics] == [
        ("skipped_nonexistent", "2026-03-08T02:30")
    ]


def test_fall_fold_uses_one_logical_id_and_canonical_due_instant():
    checkpoint = datetime(2026, 11, 1, 1, 29, tzinfo=NEW_YORK, fold=0)
    now = datetime(2026, 11, 1, 1, 31, tzinfo=NEW_YORK, fold=0)

    first = AlarmScheduler().evaluate_interval(
        [_alarm(time="01:30")], checkpoint, now, NEW_YORK
    )

    assert [item.occurrence_id for item in first.occurrences] == [
        "scheduled:1:2026-11-01:01:30"
    ]
    assert first.occurrences[0].due_at.fold == 0

    second_fold = AlarmScheduler().evaluate_interval(
        [_alarm(time="01:30")],
        datetime(2026, 11, 1, 1, 29, tzinfo=NEW_YORK, fold=1),
        datetime(2026, 11, 1, 1, 31, tzinfo=NEW_YORK, fold=1),
        NEW_YORK,
        accepted_occurrence_ids={first.occurrences[0].occurrence_id},
    )
    assert second_fold.occurrences == ()


def _snooze(alarm: Alarm, due_at: datetime, occurrence_id: str) -> Occurrence:
    return Occurrence(
        occurrence_id=occurrence_id,
        alarm_id=alarm.id,
        kind="snooze",
        due_at=due_at,
        accepted_at=None,
        alarm=AlarmSnapshot.from_alarm(alarm),
    )


def test_equal_due_candidates_sort_scheduled_then_snooze_then_alarm_id():
    due_at = datetime(2026, 7, 17, 7, 0, tzinfo=UTC)
    scheduled_b = _alarm("b")
    scheduled_a = _alarm("a")
    snoozed_a = _snooze(_alarm("a", time="08:00"), due_at, "snooze:a:one")

    result = AlarmScheduler().evaluate_interval(
        [scheduled_b, scheduled_a],
        due_at - timedelta(minutes=1),
        due_at,
        UTC,
        snoozes=[snoozed_a],
    )

    assert [(item.kind, item.alarm_id) for item in result.occurrences] == [
        ("scheduled", "a"),
        ("scheduled", "b"),
        ("snooze", "a"),
    ]
