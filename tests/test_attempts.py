from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from argus.attempts import (
    AmbiguousAttemptError,
    AttemptDecisionKind,
    AttemptExecutor,
    AttemptNotDueError,
    AttemptPolicy,
    AttemptState,
    AttemptStore,
)
from argus.model import MissionState, StepEnvelope, StepState
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerOutcome, WorkerResponse, WorkerUsage


def _prepare(path, *, due_at: datetime | None = None) -> datetime:
    due = due_at or datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.work",
                    payload={"value": 1},
                )
            ],
        )
    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", due)
    return due


def _response(
    outcome: WorkerOutcome,
    *,
    request_id: str = "mission-1/step-0/attempt-1",
    retry_after_seconds: float | None = None,
    usage: WorkerUsage | None = None,
) -> WorkerResponse:
    return WorkerResponse(
        request_id=request_id,
        outcome=outcome,
        output={"artifact_ref": "artifact://result"},
        retry_after_seconds=retry_after_seconds,
        usage=usage,
    )


def test_begin_attempt_is_durable_and_blocks_blind_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)

    with AttemptStore(path) as store:
        attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
        assert attempt.attempt_no == 1
        assert attempt.request_id == "mission-1/step-0/attempt-1"
        assert attempt.state is AttemptState.STARTED

    with SqliteMissionStore(path) as store:
        assert store.load_mission("mission-1").state is MissionState.RUNNING
        assert store.load_step("mission-1", "step-0").state is StepState.PENDING

    with AttemptStore(path) as store:
        with pytest.raises(AmbiguousAttemptError, match="replay is blocked"):
            store.begin_attempt("mission-1", "step-0", started_at=due + timedelta(seconds=1))


def test_success_persists_usage_and_atomically_finalizes_core_step(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    with AttemptStore(path) as store:
        attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
        invocation = WorkerInvocation(
            outcome=InvocationOutcome.SUCCESS,
            duration_ms=321,
            exit_code=0,
            response=_response(
                WorkerOutcome.SUCCESS,
                usage=WorkerUsage(input_tokens=100, output_tokens=25, cost_usd=0.004),
            ),
        )
        decision = store.complete_attempt(
            attempt,
            invocation,
            completed_at=due + timedelta(seconds=2),
            policy=AttemptPolicy(),
        )
        assert decision.kind is AttemptDecisionKind.SUCCEEDED
        assert decision.attempt.duration_ms == 321
        assert decision.attempt.input_tokens == 100
        assert decision.attempt.output_tokens == 25
        assert decision.attempt.cost_usd == pytest.approx(0.004)

    with SqliteMissionStore(path) as store:
        step = store.load_step("mission-1", "step-0")
        assert step.state is StepState.SUCCEEDED
        assert step.result is not None
        assert step.result.output == {"artifact_ref": "artifact://result"}
        transitions = [
            (entry.from_state, entry.to_state)
            for entry in store.journal("mission-1")
            if entry.step_id == "step-0" and entry.event_type == "state_transition"
        ]
        assert transitions[-2:] == [("pending", "running"), ("running", "succeeded")]

    with DurableScheduler(path) as scheduler:
        assert scheduler.get("mission-1", "step-0") is None
        assert scheduler.history("mission-1", "step-0")[-1].event_type == "cleared"


def test_retryable_worker_response_reschedules_without_changing_core_step(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    completed = due + timedelta(seconds=2)
    with AttemptStore(path) as store:
        attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
        decision = store.complete_attempt(
            attempt,
            WorkerInvocation(
                outcome=InvocationOutcome.RETRYABLE_FAILURE,
                duration_ms=10,
                exit_code=0,
                response=_response(
                    WorkerOutcome.RETRYABLE_FAILURE,
                    retry_after_seconds=30,
                ),
            ),
            completed_at=completed,
            policy=AttemptPolicy(max_attempts=3, retry_delay_seconds=60),
        )
        assert decision.kind is AttemptDecisionKind.RETRY_SCHEDULED
        assert decision.retry_due_at == "2026-01-01T12:00:32.000000Z"

    with SqliteMissionStore(path) as store:
        assert store.load_step("mission-1", "step-0").state is StepState.PENDING
    with DurableScheduler(path) as scheduler:
        scheduled = scheduler.get("mission-1", "step-0")
        assert scheduled is not None
        assert scheduled.due_at == "2026-01-01T12:00:32.000000Z"
        assert scheduler.due("2026-01-01T12:00:31Z") == []
        assert [item.step_id for item in scheduler.due("2026-01-01T12:00:32Z")] == ["step-0"]


def test_attempt_limit_exhaustion_fails_step_and_mission(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    policy = AttemptPolicy(max_attempts=2, retry_delay_seconds=0)

    with AttemptStore(path) as store:
        first = store.begin_attempt("mission-1", "step-0", started_at=due)
        first_decision = store.complete_attempt(
            first,
            WorkerInvocation(
                outcome=InvocationOutcome.TIMEOUT,
                duration_ms=50,
                detail="worker exceeded timeout and was terminated",
            ),
            completed_at=due + timedelta(seconds=1),
            policy=policy,
        )
        assert first_decision.kind is AttemptDecisionKind.RETRY_SCHEDULED
        second = store.begin_attempt("mission-1", "step-0", started_at=due + timedelta(seconds=1))
        final = store.complete_attempt(
            second,
            WorkerInvocation(
                outcome=InvocationOutcome.TIMEOUT,
                duration_ms=50,
                detail="worker exceeded timeout and was terminated",
            ),
            completed_at=due + timedelta(seconds=2),
            policy=policy,
        )
        assert final.kind is AttemptDecisionKind.FAILED

    with SqliteMissionStore(path) as store:
        step = store.load_step("mission-1", "step-0")
        assert step.state is StepState.FAILED
        assert step.result is not None
        assert step.result.output["error"] == "attempt_limit_exhausted:timeout"
        assert store.load_mission("mission-1").state is MissionState.FAILED


def test_permanent_malformed_and_disabled_process_retry_fail_without_reschedule(tmp_path) -> None:
    cases = [
        (InvocationOutcome.PERMANENT_FAILURE, AttemptPolicy()),
        (InvocationOutcome.MALFORMED_OUTPUT, AttemptPolicy()),
        (InvocationOutcome.PROCESS_ERROR, AttemptPolicy(retry_process_errors=False)),
    ]
    for index, (outcome, policy) in enumerate(cases):
        path = tmp_path / f"case-{index}.db"
        due = _prepare(path)
        with AttemptStore(path) as store:
            attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
            response = None
            if outcome is InvocationOutcome.PERMANENT_FAILURE:
                response = _response(WorkerOutcome.PERMANENT_FAILURE)
            decision = store.complete_attempt(
                attempt,
                WorkerInvocation(outcome=outcome, duration_ms=1, response=response),
                completed_at=due + timedelta(seconds=1),
                policy=policy,
            )
            assert decision.kind is AttemptDecisionKind.FAILED
        with DurableScheduler(path) as scheduler:
            assert scheduler.get("mission-1", "step-0") is None


def test_termination_uncertain_is_durable_and_blocks_future_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    with AttemptStore(path) as store:
        attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
        decision = store.complete_attempt(
            attempt,
            WorkerInvocation(
                outcome=InvocationOutcome.TERMINATION_UNCERTAIN,
                duration_ms=100,
                detail="worker timed out and process-tree termination could not be proven",
            ),
            completed_at=due + timedelta(seconds=1),
            policy=AttemptPolicy(),
        )
        assert decision.kind is AttemptDecisionKind.BLOCKED
        with pytest.raises(AmbiguousAttemptError, match="uncertain"):
            store.begin_attempt("mission-1", "step-0", started_at=due + timedelta(seconds=2))

    with SqliteMissionStore(path) as store:
        assert store.load_step("mission-1", "step-0").state is StepState.PENDING


class _SuccessWorker:
    def __init__(self) -> None:
        self.requests = []

    def invoke(self, request):
        self.requests.append(request)
        return WorkerInvocation(
            outcome=InvocationOutcome.SUCCESS,
            duration_ms=5,
            exit_code=0,
            response=WorkerResponse(
                request_id=request.request_id,
                outcome=WorkerOutcome.SUCCESS,
                output={"ok": True},
            ),
        )


def test_executor_requires_due_eligibility_and_uses_deterministic_request_id(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    worker = _SuccessWorker()
    executor = AttemptExecutor(path)

    with pytest.raises(AttemptNotDueError):
        executor.execute_due_step(
            "mission-1",
            "step-0",
            worker,
            now=due - timedelta(microseconds=1),
        )
    assert worker.requests == []

    decision = executor.execute_due_step(
        "mission-1",
        "step-0",
        worker,
        now=due,
    )
    assert decision.kind is AttemptDecisionKind.SUCCEEDED
    assert [request.request_id for request in worker.requests] == [
        "mission-1/step-0/attempt-1"
    ]


def test_executor_blocks_open_attempt_after_restart_boundary(tmp_path) -> None:
    path = tmp_path / "argus.db"
    due = _prepare(path)
    with AttemptStore(path) as store:
        store.begin_attempt("mission-1", "step-0", started_at=due)

    with pytest.raises(AmbiguousAttemptError, match="no durable outcome"):
        AttemptExecutor(path).execute_due_step(
            "mission-1",
            "step-0",
            _SuccessWorker(),
            now=due + timedelta(seconds=1),
        )
