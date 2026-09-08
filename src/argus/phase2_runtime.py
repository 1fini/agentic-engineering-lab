"""Integrated single-process Phase 2 execution loop for ARGUS.

This module composes the durable scheduler, attempt journal, and bounded worker
boundary. It deliberately stays small: no side-effect reconciliation, distributed
leases, budgets, or consumer semantics belong here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from argus.attempts import (
    AmbiguousAttemptError,
    AttemptDecision,
    AttemptDecisionKind,
    AttemptError,
    AttemptExecutor,
    AttemptNotDueError,
    AttemptPolicy,
    AttemptState,
    AttemptStore,
    WorkerInvoker,
)
from argus.model import MissionState, StepState
from argus.scheduler import DurableScheduler, ScheduledStep, canonical_due_at
from argus.store import SqliteMissionStore
from argus.worker_protocol import WorkerRequest


class WorkerRouteError(AttemptError):
    """Raised when no bounded worker is configured for a due operation."""


class Phase2Runtime:
    """Deterministically execute durably due work in the local Phase 2 runtime.

    `AttemptExecutor` remains the low-level one-step primitive. This integration
    layer adds two cross-cutting properties required by the Phase 2 vertical slice:

    - an injected `now` is the clock boundary for both eligibility and retry timing;
    - a mission whose final logical step succeeds is deterministically reconciled to
      `COMPLETED`, including after a crash between step commit and mission completion.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def due(self, now: str | datetime | None = None) -> list[ScheduledStep]:
        instant = canonical_due_at(now or datetime.now(timezone.utc))
        with DurableScheduler(self.path) as scheduler:
            return scheduler.due(instant)

    def run_next_due(
        self,
        workers: Mapping[str, WorkerInvoker],
        *,
        policy: AttemptPolicy = AttemptPolicy(),
        now: str | datetime | None = None,
    ) -> AttemptDecision | None:
        """Execute the first deterministic due item, or return `None` when idle."""
        instant = canonical_due_at(now or datetime.now(timezone.utc))
        due = self.due(instant)
        if not due:
            return None

        target = due[0]
        with SqliteMissionStore(self.path) as store:
            step = store.load_step(target.mission_id, target.step_id)
        try:
            worker = workers[step.envelope.operation]
        except KeyError as exc:
            raise WorkerRouteError(
                f"no bounded worker configured for operation: {step.envelope.operation}"
            ) from exc

        return self.execute_due_step(
            target.mission_id,
            target.step_id,
            worker,
            policy=policy,
            now=instant,
        )

    def execute_due_step(
        self,
        mission_id: str,
        step_id: str,
        worker: WorkerInvoker,
        *,
        policy: AttemptPolicy = AttemptPolicy(),
        now: str | datetime | None = None,
    ) -> AttemptDecision:
        """Execute one due step using a single explicit logical clock boundary."""
        instant = canonical_due_at(now or datetime.now(timezone.utc))

        # Safe reconciliation can repair the narrow crash boundary after a final
        # step result committed but before the mission state was advanced.
        self.reconcile_mission(mission_id)

        with DurableScheduler(self.path) as scheduler:
            eligible = {
                (item.mission_id, item.step_id) for item in scheduler.due(instant)
            }
        if (mission_id, step_id) not in eligible:
            raise AttemptNotDueError(
                f"step {mission_id}/{step_id} is not durably eligible at {instant}"
            )

        with AttemptStore(self.path) as store:
            latest = store.latest_attempt(mission_id, step_id)
            if latest is not None and latest.state is AttemptState.STARTED:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has no durable outcome; replay is blocked"
                )
            from argus.worker_adapter import InvocationOutcome

            if latest is not None and latest.outcome is InvocationOutcome.TERMINATION_UNCERTAIN:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has uncertain termination; replay is blocked"
                )
            attempt = store.begin_attempt(mission_id, step_id, started_at=instant)

        # The STARTED attempt is durable before any worker invocation. A crash from
        # here onward therefore leaves evidence that blocks blind replay.
        with SqliteMissionStore(self.path) as mission_store:
            step = mission_store.load_step(mission_id, step_id)
        request = WorkerRequest(
            request_id=attempt.request_id,
            operation=step.envelope.operation,
            payload=step.envelope.payload,
            payload_version=step.envelope.payload_version,
        )
        invocation = worker.invoke(request)

        # When the caller injects a clock boundary, keep retries reproducible by
        # completing the attempt at that same logical instant. In real operation,
        # no explicit `now` is supplied and wall-clock completion is used.
        completed_at: str | datetime
        if now is None:
            completed_at = datetime.now(timezone.utc)
        else:
            completed_at = instant

        with AttemptStore(self.path) as store:
            decision = store.complete_attempt(
                attempt,
                invocation,
                completed_at=completed_at,
                policy=policy,
            )

        if decision.kind is AttemptDecisionKind.SUCCEEDED:
            self.reconcile_mission(mission_id)
        return decision

    def reconcile_mission(self, mission_id: str) -> MissionState:
        """Safely complete a mission when all logical steps are already succeeded.

        This reconciliation never replays workers and therefore remains safe after
        process death. It closes the crash boundary between final step commit and
        mission completion without pretending to reconcile ambiguous external
        effects.
        """
        with SqliteMissionStore(self.path) as store:
            mission = store.load_mission(mission_id)
            if mission.state in {MissionState.COMPLETED, MissionState.FAILED}:
                return mission.state
            steps = store.list_steps(mission_id)
            if steps and all(step.state is StepState.SUCCEEDED for step in steps):
                if mission.state is MissionState.PENDING:
                    mission = store.transition_mission(mission_id, MissionState.RUNNING)
                mission = store.transition_mission(mission_id, MissionState.COMPLETED)
            return mission.state


__all__ = [
    "Phase2Runtime",
    "WorkerRouteError",
    # Re-exported for callers migrating from the lower-level Phase 2 primitives.
    "AttemptExecutor",
]
