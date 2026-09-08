"""Deterministic single-process runner for ARGUS Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from argus.model import (
    ArgusStateError,
    MissionState,
    StepEnvelope,
    StepResult,
    StepResultKind,
    StepState,
)
from argus.store import SqliteMissionStore

Worker = Callable[[StepEnvelope], Mapping[str, Any]]


class BlockedMissionError(ArgusStateError):
    """Raised when durable state cannot safely make forward progress."""


class WorkerInterruptedError(BlockedMissionError):
    """Raised when a worker exits unexpectedly after a step was marked RUNNING."""


class StepExecutionFailure(RuntimeError):
    """Explicit deterministic worker failure that may be persisted as terminal."""

    def __init__(self, message: str, *, output: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.output = dict(output or {})
        self.output.setdefault("message", message)


class WorkerRegistry:
    """Explicit operation-to-callable mapping used by the Phase 1 runner."""

    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, operation: str, worker: Worker) -> None:
        if not operation:
            raise ValueError("operation must be non-empty")
        if operation in self._workers:
            raise ValueError(f"worker already registered for operation: {operation}")
        self._workers[operation] = worker

    def resolve(self, operation: str) -> Worker:
        try:
            return self._workers[operation]
        except KeyError as exc:
            raise BlockedMissionError(
                f"no worker registered for operation: {operation}"
            ) from exc


@dataclass(frozen=True)
class RunSummary:
    mission_id: str
    state: MissionState
    executed_steps: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "state": self.state.value,
            "executed_steps": list(self.executed_steps),
        }


class MissionRunner:
    """Run the next durable Phase 1 work deterministically to a terminal/block state."""

    def __init__(self, store: SqliteMissionStore, workers: WorkerRegistry) -> None:
        self.store = store
        self.workers = workers

    def run(self, mission_id: str) -> RunSummary:
        mission = self.store.load_mission(mission_id)
        executed: list[str] = []

        if mission.state in {MissionState.COMPLETED, MissionState.FAILED}:
            return RunSummary(mission_id, mission.state, tuple(executed))

        if mission.state is MissionState.PENDING:
            mission = self.store.transition_mission(mission_id, MissionState.RUNNING)

        for step in self.store.list_steps(mission_id):
            if step.state is StepState.SUCCEEDED:
                continue
            if step.state is StepState.FAILED:
                failed = self.store.transition_mission(mission_id, MissionState.FAILED)
                return RunSummary(mission_id, failed.state, tuple(executed))
            if step.state is StepState.RUNNING:
                raise BlockedMissionError(
                    f"step {step.envelope.step_id!r} is already RUNNING; "
                    "recovery semantics are handled by Mission #1 workstream #5"
                )

            worker = self.workers.resolve(step.envelope.operation)
            self.store.transition_step(
                mission_id,
                step.envelope.step_id,
                StepState.RUNNING,
            )
            executed.append(step.envelope.step_id)

            try:
                output = worker(step.envelope)
            except StepExecutionFailure as exc:
                failure = StepResult(
                    kind=StepResultKind.FAILURE,
                    output={"error": "step_failure", **exc.output},
                )
                self.store.transition_step(
                    mission_id,
                    step.envelope.step_id,
                    StepState.FAILED,
                    result=failure,
                )
                failed = self.store.transition_mission(mission_id, MissionState.FAILED)
                return RunSummary(mission_id, failed.state, tuple(executed))
            except Exception as exc:
                raise WorkerInterruptedError(
                    f"worker for step {step.envelope.step_id!r} did not return a durable result; "
                    "step remains RUNNING"
                ) from exc

            try:
                success = StepResult(kind=StepResultKind.SUCCESS, output=output)
            except (TypeError, ValueError) as exc:
                failure = StepResult(
                    kind=StepResultKind.FAILURE,
                    output={
                        "error": "invalid_worker_output",
                        "message": str(exc),
                    },
                )
                self.store.transition_step(
                    mission_id,
                    step.envelope.step_id,
                    StepState.FAILED,
                    result=failure,
                )
                failed = self.store.transition_mission(mission_id, MissionState.FAILED)
                return RunSummary(mission_id, failed.state, tuple(executed))

            self.store.transition_step(
                mission_id,
                step.envelope.step_id,
                StepState.SUCCEEDED,
                result=success,
            )

        completed = self.store.transition_mission(mission_id, MissionState.COMPLETED)
        return RunSummary(mission_id, completed.state, tuple(executed))
