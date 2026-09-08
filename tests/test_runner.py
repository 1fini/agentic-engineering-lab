from __future__ import annotations

import pytest

from argus.model import MissionState, StepEnvelope, StepState
from argus.runner import (
    BlockedMissionError,
    MissionRunner,
    StepExecutionFailure,
    WorkerInterruptedError,
    WorkerRegistry,
)
from argus.store import SqliteMissionStore


def _three_steps(mission_id: str = "mission-1") -> list[StepEnvelope]:
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


def test_runner_completes_fixture_mission_and_rerun_is_noop(tmp_path) -> None:
    calls: list[str] = []
    registry = WorkerRegistry()

    def worker(step: StepEnvelope):
        calls.append(step.step_id)
        return {"handled": step.step_id}

    registry.register("fixture.work", worker)
    with SqliteMissionStore(tmp_path / "argus.db") as store:
        store.create_mission("mission-1", _three_steps())
        runner = MissionRunner(store, registry)

        first = runner.run("mission-1")
        assert first.state is MissionState.COMPLETED
        assert first.executed_steps == ("step-0", "step-1", "step-2")
        assert calls == ["step-0", "step-1", "step-2"]

        second = runner.run("mission-1")
        assert second.state is MissionState.COMPLETED
        assert second.executed_steps == ()
        assert calls == ["step-0", "step-1", "step-2"]


def test_unknown_operation_blocks_before_step_execution(tmp_path) -> None:
    with SqliteMissionStore(tmp_path / "argus.db") as store:
        store.create_mission("mission-1", _three_steps())
        with pytest.raises(BlockedMissionError):
            MissionRunner(store, WorkerRegistry()).run("mission-1")
        assert store.load_mission("mission-1").state is MissionState.RUNNING
        assert store.load_step("mission-1", "step-0").state is StepState.PENDING


def test_explicit_worker_failure_is_persisted_terminally(tmp_path) -> None:
    registry = WorkerRegistry()

    def fail(step: StepEnvelope):
        raise StepExecutionFailure("expected failure", output={"step": step.step_id})

    registry.register("fixture.work", fail)
    with SqliteMissionStore(tmp_path / "argus.db") as store:
        store.create_mission("mission-1", _three_steps())
        summary = MissionRunner(store, registry).run("mission-1")
        assert summary.state is MissionState.FAILED
        step = store.load_step("mission-1", "step-0")
        assert step.state is StepState.FAILED
        assert step.result is not None
        assert step.result.output["error"] == "step_failure"
        assert store.load_mission("mission-1").state is MissionState.FAILED


def test_unexpected_worker_exception_leaves_step_running_for_recovery(tmp_path) -> None:
    registry = WorkerRegistry()

    def crash(step: StepEnvelope):
        raise RuntimeError(f"boom: {step.step_id}")

    registry.register("fixture.work", crash)
    with SqliteMissionStore(tmp_path / "argus.db") as store:
        store.create_mission("mission-1", _three_steps())
        runner = MissionRunner(store, registry)
        with pytest.raises(WorkerInterruptedError):
            runner.run("mission-1")
        assert store.load_mission("mission-1").state is MissionState.RUNNING
        assert store.load_step("mission-1", "step-0").state is StepState.RUNNING

        with pytest.raises(BlockedMissionError):
            runner.run("mission-1")


def test_non_serializable_worker_output_fails_deterministically(tmp_path) -> None:
    registry = WorkerRegistry()

    def invalid_output(step: StepEnvelope):
        return {"not_json": object()}

    registry.register("fixture.work", invalid_output)
    with SqliteMissionStore(tmp_path / "argus.db") as store:
        store.create_mission("mission-1", _three_steps())
        summary = MissionRunner(store, registry).run("mission-1")
        assert summary.state is MissionState.FAILED
        step = store.load_step("mission-1", "step-0")
        assert step.state is StepState.FAILED
        assert step.result is not None
        assert step.result.output["error"] == "invalid_worker_output"
