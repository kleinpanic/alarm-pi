import json
import multiprocessing
import os
from pathlib import Path
import signal

import pytest

from alarm.models import Alarm, RuntimeData, Settings
from alarm.repository import OperationalRepository


def _alarm(alarm_id="99", label="wake"):
    return Alarm(id=alarm_id, label=label, time="07:00", days_of_week=[0])


def _create(root, proposed, queue):
    created = OperationalRepository(Path(root)).create_alarm(_alarm(proposed, proposed))
    queue.put(created.id)


def _rename(root, alarm_id, label):
    repo = OperationalRepository(Path(root))
    alarm = next(item for item in repo.snapshot().alarms if item.id == alarm_id)
    alarm.label = label
    assert repo.update_alarm(alarm)


def _hard_kill_before_replace(root, ready):
    class KillingRepository(OperationalRepository):
        def _before_replace(self, _temp_path):
            ready.set()
            os.kill(os.getpid(), signal.SIGKILL)

    KillingRepository(Path(root)).create_alarm(_alarm())


def test_empty_and_equal_proposed_ids_are_allocated_inside_transaction(tmp_path):
    repo = OperationalRepository(tmp_path)
    assert repo.create_alarm(_alarm("7")).id == "1"
    assert repo.create_alarm(_alarm("7")).id == "2"
    assert [alarm.id for alarm in repo.snapshot().alarms] == ["1", "2"]


def test_snapshots_are_detached_and_idempotent_mutations_do_not_increment(tmp_path):
    repo = OperationalRepository(tmp_path)
    created = repo.create_alarm(_alarm())
    revision = repo.snapshot().revision
    first = repo.snapshot()
    second = repo.snapshot()
    assert first == second
    first.alarms[0].label = "memory only"
    assert repo.snapshot().alarms[0].label == "wake"
    assert repo.update_alarm(created) is True
    assert repo.snapshot().revision == revision


def test_alarm_settings_and_runtime_changes_share_one_aggregate_revision(tmp_path):
    repo = OperationalRepository(tmp_path)
    assert repo.snapshot().revision == 0
    repo.create_alarm(_alarm())
    assert repo.snapshot().revision == 1
    repo.set_settings(Settings(default_snooze_minutes=9))
    assert repo.snapshot().revision == 2
    repo.set_runtime(RuntimeData(scheduler_checkpoint="2026-07-17T00:00:00+00:00"))
    assert repo.snapshot().revision == 3
    repo.set_runtime(RuntimeData(scheduler_checkpoint="2026-07-17T00:00:00+00:00"))
    assert repo.snapshot().revision == 3


def test_multiprocess_creates_and_edits_do_not_lose_updates(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    creators = [ctx.Process(target=_create, args=(str(tmp_path), "5", queue)) for _ in range(8)]
    for child in creators:
        child.start()
    for child in creators:
        child.join(10)
        assert child.exitcode == 0
    ids = sorted((queue.get(timeout=2) for _ in creators), key=int)
    assert ids == [str(index) for index in range(1, 9)]
    assert len(json.loads((tmp_path / "operational.json").read_text())["alarms"]) == 8

    editors = [ctx.Process(target=_rename, args=(str(tmp_path), str(i), f"edited-{i}")) for i in range(1, 9)]
    for child in editors:
        child.start()
    for child in editors:
        child.join(10)
        assert child.exitcode == 0
    assert {a.label for a in OperationalRepository(tmp_path).snapshot().alarms} == {
        f"edited-{i}" for i in range(1, 9)
    }


def test_caught_pre_replace_failure_preserves_bytes_and_cleans_temp(tmp_path, monkeypatch):
    repo = OperationalRepository(tmp_path)
    repo.create_alarm(_alarm())
    before = repo.operational_file.read_bytes()

    def fail(_path):
        raise RuntimeError("injected")

    monkeypatch.setattr(repo, "_before_replace", fail)
    with pytest.raises(RuntimeError, match="injected"):
        repo.create_alarm(_alarm())
    assert repo.operational_file.read_bytes() == before
    assert list(tmp_path.glob(".operational.json.*.tmp")) == []


def test_hard_kill_orphan_is_cleaned_selectively_on_next_load(tmp_path):
    repo = OperationalRepository(tmp_path)
    repo.create_alarm(_alarm())
    before = repo.operational_file.read_bytes()
    unrelated = tmp_path / ".operational.json.unrelated.txt"
    unrelated.write_text("keep")
    symlink = tmp_path / ".operational.json.fake.tmp"
    symlink.symlink_to(unrelated)

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    child = ctx.Process(target=_hard_kill_before_replace, args=(str(tmp_path), ready))
    child.start()
    assert ready.wait(10)
    child.join(10)
    assert child.exitcode == -signal.SIGKILL
    assert json.loads(repo.operational_file.read_text())["revision"] == 1
    assert repo.operational_file.read_bytes() == before
    owned = [p for p in tmp_path.glob(".operational.json.*.tmp") if not p.is_symlink()]
    assert len(owned) == 1

    repo.snapshot()
    assert not owned[0].exists()
    assert unrelated.read_text() == "keep"
    assert symlink.is_symlink()
