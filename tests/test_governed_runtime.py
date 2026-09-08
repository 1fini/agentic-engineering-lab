from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from argus.attempts import AmbiguousAttemptError, AttemptDecisionKind, AttemptStore
from argus.budgets import BudgetPolicy, BudgetStore, ReservationState
from argus.effect_attempts import EffectAttemptStore
from argus.effect_runtime import (
    EffectExecutionOutcome,
    EffectExecutionResult,
    EffectRuntimeDecision,
)
from argus.effects import AmbiguousEffectError, EffectIntent, EffectState, EffectStore
from argus.governed_runtime import ExecutionGateDenied, GovernedRuntime
from argus.guardrails import GuardrailPolicy, GuardrailStore
from argus.model import StepEnvelope
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerOutcome, WorkerResponse, WorkerUsage

_DUE = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _prepare(path, *, worker_limit=None, effect_limit=None, spend_limit=None) -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.remote",
                    payload={"input_ref": "artifact://opaque"},
                )
            ],
        )
    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", _DUE)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(
            GuardrailPolicy(
                mission_id="mission-1",
                workload_scope="workload-alpha",
            )
        )
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(
                mission_id="mission-1",
                worker_attempt_limit=worker_limit,
                effect_attempt_limit=effect_limit,
                spend_limit_usd=spend_limit,
            )
        )


class CountingWorker:
    def __init__(self, *, cost_usd: float | None = 0.25, crash: bool = False) -> None:
        self.cost_usd = cost_usd
        self.crash = crash
        self.calls = 0

    def invoke(self, request):
        self.calls += 1
        if self.crash:
            raise RuntimeError("fixture worker crashed")
        return WorkerInvocation(
            outcome=InvocationOutcome.SUCCESS,
            duration_ms=5,
            response=WorkerResponse(
                request_id=request.request_id,
                outcome=WorkerOutcome.SUCCESS,
                output={"artifact_ref": "artifact://result"},
                usage=WorkerUsage(
                    input_tokens=3,
                    output_tokens=2,
                    cost_usd=self.cost_usd,
                ),
            ),
            exit_code=0,
        )


class CountingEffectExecutor:
    def __init__(self, outcome: EffectExecutionOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    def execute(self, context):
        self.calls += 1
        return EffectExecutionResult(
            correlation_key=context.effect.intent.correlation_key or "",
            attempt_key=context.attempt.attempt_key,
            outcome=self.outcome,
            evidence={"remote_ref": "opaque://remote/result"},
        )


def _create_effect(path) -> None:
    with EffectStore(path) as effects:
        effects.create_intent(
            EffectIntent(
                mission_id="mission-1",
                step_id="step-0",
                effect_id="effect-0",
                operation="external.apply",
                payload={"artifact_ref": "artifact://candidate"},
            )
        )


def test_pause_denies_before_worker_attempt_then_resume_allows(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, worker_limit=2)
    worker = CountingWorker()
    with GuardrailStore(path) as guardrails:
        guardrails.pause("mission-1")

    runtime = GovernedRuntime(path)
    with pytest.raises(ExecutionGateDenied, match="mission_paused"):
        runtime.execute_due_worker("mission-1", "step-0", worker, now=_DUE)
    assert worker.calls == 0
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0") == []

    with GuardrailStore(path) as guardrails:
        guardrails.resume("mission-1")
    decision = runtime.execute_due_worker("mission-1", "step-0", worker, now=_DUE)
    assert decision.kind is AttemptDecisionKind.SUCCEEDED
    assert worker.calls == 1


def test_global_and_workload_kill_deny_without_attempt_allocation(tmp_path) -> None:
    for mode in ("workload", "global"):
        path = tmp_path / f"{mode}.db"
        _prepare(path, worker_limit=2)
        worker = CountingWorker()
        with GuardrailStore(path) as guardrails:
            if mode == "global":
                guardrails.set_global_kill(True)
            else:
                guardrails.set_workload_kill("workload-alpha", True)
        with pytest.raises(ExecutionGateDenied, match=f"{mode}_kill"):
            GovernedRuntime(path).execute_due_worker(
                "mission-1", "step-0", worker, now=_DUE
            )
        assert worker.calls == 0
        with AttemptStore(path) as attempts:
            assert attempts.list_attempts("mission-1", "step-0") == []


def test_worker_attempt_budget_denies_before_attempt_and_invocation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, worker_limit=0)
    worker = CountingWorker()
    runtime = GovernedRuntime(path)
    with pytest.raises(ExecutionGateDenied, match="worker_attempt_exhausted"):
        runtime.execute_due_worker("mission-1", "step-0", worker, now=_DUE)
    assert worker.calls == 0
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0") == []
    events = runtime.events("mission-1")
    assert events[-1].decision == "deny"
    assert events[-1].reason == "worker_attempt_exhausted"


def test_worker_spend_is_reserved_before_call_and_committed_from_usage(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, worker_limit=2, spend_limit="1")
    worker = CountingWorker(cost_usd=0.25)
    runtime = GovernedRuntime(path)

    decision = runtime.execute_due_worker(
        "mission-1",
        "step-0",
        worker,
        now=_DUE,
        reserve_spend_usd="0.50",
    )
    assert decision.kind is AttemptDecisionKind.SUCCEEDED
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.COMMITTED
        assert reservations[0].reserved_usd == Decimal("0.5")
        assert reservations[0].committed_usd == Decimal("0.25")
        assert budgets.snapshot("mission-1").spend_available_usd == Decimal("0.75")


def test_configured_spend_budget_requires_pre_call_reservation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, worker_limit=2, spend_limit="1")
    worker = CountingWorker()
    with pytest.raises(ExecutionGateDenied, match="spend_reservation_required"):
        GovernedRuntime(path).execute_due_worker(
            "mission-1", "step-0", worker, now=_DUE
        )
    assert worker.calls == 0


def test_worker_crash_keeps_reservation_and_started_attempt_fail_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, worker_limit=3, spend_limit="2")
    worker = CountingWorker(crash=True)
    runtime = GovernedRuntime(path)

    with pytest.raises(RuntimeError, match="fixture worker crashed"):
        runtime.execute_due_worker(
            "mission-1",
            "step-0",
            worker,
            now=_DUE,
            reserve_spend_usd="0.75",
        )
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RESERVED
    with AttemptStore(path) as attempts:
        assert attempts.latest_attempt("mission-1", "step-0").state.value == "started"

    with pytest.raises(AmbiguousAttemptError):
        runtime.execute_due_worker(
            "mission-1",
            "step-0",
            worker,
            now=_DUE,
            reserve_spend_usd="0.75",
        )
    assert worker.calls == 1
    with BudgetStore(path) as budgets:
        assert len(budgets.list_reservations("mission-1")) == 1


def test_effect_budget_denies_before_effect_attempt_or_external_call(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, effect_limit=0)
    _create_effect(path)
    executor = CountingEffectExecutor(EffectExecutionOutcome.APPLIED)

    with pytest.raises(ExecutionGateDenied, match="effect_attempt_exhausted"):
        GovernedRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", executor, now=_DUE
        )
    assert executor.calls == 0
    with EffectStore(path) as effects:
        assert effects.load("mission-1", "step-0", "effect-0").state is EffectState.INTENT_COMMITTED
    with EffectAttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0", "effect-0") == []


def test_ambiguous_effect_does_not_allocate_second_budget_or_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, effect_limit=3, spend_limit="2")
    _create_effect(path)
    executor = CountingEffectExecutor(EffectExecutionOutcome.UNKNOWN)
    runtime = GovernedRuntime(path)

    result = runtime.execute_effect(
        "mission-1",
        "step-0",
        "effect-0",
        executor,
        now=_DUE,
        reserve_spend_usd="0.5",
    )
    assert result.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert executor.calls == 1

    with pytest.raises(AmbiguousEffectError):
        runtime.execute_effect(
            "mission-1",
            "step-0",
            "effect-0",
            executor,
            now=_DUE,
            reserve_spend_usd="0.5",
        )
    assert executor.calls == 1
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RESERVED
    with EffectAttemptStore(path) as attempts:
        assert len(attempts.list_attempts("mission-1", "step-0", "effect-0")) == 1


def test_confirmed_not_applied_reattempt_uses_next_effect_budget_slot(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, effect_limit=2)
    _create_effect(path)
    first = CountingEffectExecutor(EffectExecutionOutcome.NOT_APPLIED)
    second = CountingEffectExecutor(EffectExecutionOutcome.APPLIED)
    runtime = GovernedRuntime(path)

    result1 = runtime.execute_effect(
        "mission-1", "step-0", "effect-0", first, now=_DUE
    )
    assert result1.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
    result2 = runtime.reattempt_effect(
        "mission-1", "step-0", "effect-0", second, now=_DUE
    )
    assert result2.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert first.calls == 1 and second.calls == 1
    with EffectAttemptStore(path) as attempts:
        history = attempts.list_attempts("mission-1", "step-0", "effect-0")
        assert [item.attempt_no for item in history] == [1, 2]


def test_effect_spend_can_be_committed_from_generic_cost_resolver(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path, effect_limit=1, spend_limit="1")
    _create_effect(path)
    executor = CountingEffectExecutor(EffectExecutionOutcome.APPLIED)

    result = GovernedRuntime(path).execute_effect(
        "mission-1",
        "step-0",
        "effect-0",
        executor,
        now=_DUE,
        reserve_spend_usd="0.4",
        cost_resolver=lambda _: Decimal("0.3"),
    )
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    with BudgetStore(path) as budgets:
        reservation = budgets.list_reservations("mission-1")[0]
        assert reservation.state is ReservationState.COMMITTED
        assert reservation.committed_usd == Decimal("0.3")


def test_governed_fixture_values_remain_domain_opaque() -> None:
    values = "mission-1 workload-alpha fixture.remote external.apply artifact://opaque"
    forbidden = ("youtube", "short", "retention", "editorial", "publication")
    for term in forbidden:
        assert term not in values.lower()
