from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from argus.model import MissionState, StepEnvelope, StepResult, StepResultKind, StepState
from argus.scheduler import DurableScheduler, ScheduleError, canonical_due_at
from argus.store import SqliteMissionStore


def _steps(mission_id: str = "mission-1") -> list[StepEnvelope]:
    return [
        StepEnvelope(
            mission_id=mission_id,
            step_id=f"step-{index}",
            ordinal=index,
            operation="fixture.work",
            payload={"index": index},
        )
        for index in range(3)
    ]


def _create_store(path) -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())


def test_canonical_due_at_requires_timezone_and_normalizes_utc() -> None:
    assert (
        canonical_due_at("2026-09-08T13:00:00+02:00")
        == "2026-09-08T11:00:00.000000Z"
    )
    with pytest.raises(ValueError, match="timezone"):
        canonical_due_at("2026-09-08T11:00:00")
    with pytest.raises(ValueError, match="invalid"):
        canonical_due_at("not-a-time")


def test_schedule_survives_restart_and_is_not_due_early(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    due_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    with DurableScheduler(path) as scheduler:
        scheduled = scheduler.schedule("mission-1", "step-0", due_at)
        assert scheduled.due_at == "2026-09-08T12:00:00.000000Z"
        assert scheduler.due(due_at - timedelta(microseconds=1)) == []

    with DurableScheduler(path) as scheduler:
        assert [item.step_id for item in scheduler.due(due_at)] == ["step-0"]


def test_duplicate_schedule_and_due_scans_are_idempotent(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    due_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    with DurableScheduler(path) as scheduler:
        first = scheduler.schedule("mission-1", "step-0", due_at)
        second = scheduler.schedule("mission-1", "step-0", due_at)
        assert first == second
        assert len(scheduler.history("mission-1", "step-0")) == 1

        first_scan = scheduler.due(due_at)
        second_scan = scheduler.due(due_at)
        assert first_scan == second_scan
        assert len(scheduler.history("mission-1", "step-0")) == 1

    with SqliteMissionStore(path) as store:
        assert store.load_step("mission-1", "step-0").state is StepState.PENDING
        assert store.load_mission("mission-1").state is MissionState.PENDING


def test_reschedule_is_audited_and_pending_only(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    first_due = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    second_due = first_due + timedelta(hours=1)

    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", first_due)
        scheduler.schedule("mission-1", "step-0", second_due)
        events = scheduler.history("mission-1", "step-0")
        assert [event.event_type for event in events] == ["scheduled", "rescheduled"]
        assert events[-1].previous_due_at == "2026-09-08T12:00:00.000000Z"
        assert events[-1].due_at == "2026-09-08T13:00:00.000000Z"

    with SqliteMissionStore(path) as store:
        store.transition_mission("mission-1", MissionState.RUNNING)
        store.transition_step("mission-1", "step-0", StepState.RUNNING)
        store.transition_step(
            "mission-1",
            "step-0",
            StepState.SUCCEEDED,
            result=StepResult(kind=StepResultKind.SUCCESS, output={"ok": True}),
        )

    with DurableScheduler(path) as scheduler:
        with pytest.raises(ScheduleError, match="only pending"):
            scheduler.schedule("mission-1", "step-0", second_due)


def test_due_scheduler_respects_step_order_dependencies(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    due_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", due_at)
        scheduler.schedule("mission-1", "step-1", due_at)
        assert [item.step_id for item in scheduler.due(due_at)] == ["step-0"]

    with SqliteMissionStore(path) as store:
        store.transition_mission("mission-1", MissionState.RUNNING)
        store.transition_step("mission-1", "step-0", StepState.RUNNING)
        store.transition_step(
            "mission-1",
            "step-0",
            StepState.SUCCEEDED,
            result=StepResult(kind=StepResultKind.SUCCESS, output={"ok": True}),
        )

    with DurableScheduler(path) as scheduler:
        assert [item.step_id for item in scheduler.due(due_at)] == ["step-1"]


def test_unknown_or_noninitialized_store_fails_closed(tmp_path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(ScheduleError, match="initialized ARGUS mission store"):
        DurableScheduler(path)

    path = tmp_path / "argus.db"
    _create_store(path)
    with DurableScheduler(path) as scheduler:
        with pytest.raises(KeyError, match="unknown step"):
            scheduler.schedule(
                "mission-1",
                "missing",
                datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
            )


def test_phase1_store_remains_readable_after_schedule_extension(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    with DurableScheduler(path) as scheduler:
        scheduler.schedule(
            "mission-1",
            "step-0",
            datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        )

    with SqliteMissionStore(path) as store:
        assert store.load_mission("mission-1").state is MissionState.PENDING
        assert [step.envelope.step_id for step in store.list_steps("mission-1")] == [
            "step-0",
            "step-1",
            "step-2",
        ]


def test_corrupt_persisted_due_at_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_store(path)
    with DurableScheduler(path) as scheduler:
        scheduler.schedule(
            "mission-1",
            "step-0",
            datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc),
        )

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE scheduled_steps SET due_at = 'not-a-time' WHERE mission_id = 'mission-1' AND step_id = 'step-0'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(ScheduleError, match="invalid persisted due_at"):
        DurableScheduler(path)
