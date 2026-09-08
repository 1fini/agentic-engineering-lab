from __future__ import annotations

from dataclasses import dataclass

import pytest

from argus.effect_runtime import (
    EffectContractError,
    EffectExecutionOutcome,
    EffectExecutionResult,
    EffectRuntime,
    EffectRuntimeDecision,
    ReconciliationDecision,
    ReconciliationResult,
)
from argus.effects import (
    AmbiguousEffectError,
    EffectError,
    EffectIntent,
    EffectReceiptSource,
    EffectState,
    EffectStore,
)
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _prepare(path) -> EffectIntent:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.effect",
                    payload={"input_ref": "artifact://input"},
                )
            ],
        )
    intent = EffectIntent(
        mission_id="mission-1",
        step_id="step-0",
        effect_id="effect-0",
        operation="external.apply",
        payload={"artifact_ref": "artifact://candidate"},
    )
    with EffectStore(path) as store:
        store.create_intent(intent)
    return intent


@dataclass
class _Executor:
    path: object
    outcome: EffectExecutionOutcome
    calls: int = 0

    def execute(self, intent):
        self.calls += 1
        with EffectStore(self.path) as store:
            durable = store.load(intent.mission_id, intent.step_id, intent.effect_id)
            assert durable.state is EffectState.OUTCOME_UNKNOWN
        return EffectExecutionResult(
            correlation_key=intent.correlation_key,
            outcome=self.outcome,
            evidence={"remote_ref": "remote://opaque"},
        )


@dataclass
class _Reconciler:
    decision: ReconciliationDecision
    calls: int = 0

    def reconcile(self, effect):
        self.calls += 1
        assert effect.state is EffectState.OUTCOME_UNKNOWN
        return ReconciliationResult(
            correlation_key=effect.intent.correlation_key,
            decision=self.decision,
            evidence={"lookup_ref": "evidence://opaque"},
        )


def test_executor_is_called_only_after_unknown_boundary_is_durable(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, EffectExecutionOutcome.APPLIED)

    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", executor
    )

    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert result.effect.state is EffectState.CONFIRMED_APPLIED
    assert result.effect.receipt is not None
    assert result.effect.receipt.source is EffectReceiptSource.EXECUTION


def test_executor_not_applied_is_confirmed_without_retrying(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, EffectExecutionOutcome.NOT_APPLIED)

    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", executor
    )

    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
    assert result.effect.state is EffectState.CONFIRMED_NOT_APPLIED


def test_executor_unknown_leaves_effect_blocked_and_second_execute_is_forbidden(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, EffectExecutionOutcome.UNKNOWN)
    runtime = EffectRuntime(path)

    result = runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    assert result.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert result.effect.state is EffectState.OUTCOME_UNKNOWN
    assert executor.calls == 1

    with pytest.raises(AmbiguousEffectError, match="reconcile before replay"):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    assert executor.calls == 1


def test_executor_exception_leaves_durable_unknown_and_blocks_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class ExplodingExecutor:
        calls = 0

        def execute(self, intent):
            self.calls += 1
            raise RuntimeError("external boundary crashed")

    executor = ExplodingExecutor()
    runtime = EffectRuntime(path)
    with pytest.raises(RuntimeError, match="external boundary crashed"):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN
    with pytest.raises(AmbiguousEffectError):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    assert executor.calls == 1


def test_execution_correlation_mismatch_fails_closed_in_unknown_state(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class WrongCorrelation:
        def execute(self, intent):
            return EffectExecutionResult(
                correlation_key="argus:effect:v1:" + "0" * 64,
                outcome=EffectExecutionOutcome.APPLIED,
                evidence={},
            )

    with pytest.raises(EffectContractError, match="does not match"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", WrongCorrelation()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN


def test_reconciliation_applied_commits_receipt_without_executor_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, EffectExecutionOutcome.UNKNOWN)
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    reconciler = _Reconciler(ReconciliationDecision.CONFIRMED_APPLIED)
    result = runtime.reconcile_effect(
        "mission-1", "step-0", "effect-0", reconciler
    )

    assert executor.calls == 1
    assert reconciler.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert result.effect.receipt is not None
    assert result.effect.receipt.source is EffectReceiptSource.RECONCILIATION


def test_reconciliation_not_applied_is_typed_and_does_not_reexecute(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, EffectExecutionOutcome.UNKNOWN)
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    result = runtime.reconcile_effect(
        "mission-1",
        "step-0",
        "effect-0",
        _Reconciler(ReconciliationDecision.CONFIRMED_NOT_APPLIED),
    )
    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
    assert result.effect.state is EffectState.CONFIRMED_NOT_APPLIED


def test_still_unknown_reconciliation_can_be_repeated_without_effect_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, EffectExecutionOutcome.UNKNOWN)
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    reconciler = _Reconciler(ReconciliationDecision.STILL_UNKNOWN)

    first = runtime.reconcile_effect("mission-1", "step-0", "effect-0", reconciler)
    second = runtime.reconcile_effect("mission-1", "step-0", "effect-0", reconciler)

    assert first.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert second.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert reconciler.calls == 2
    assert executor.calls == 1


def test_reconciliation_requires_unknown_state(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    reconciler = _Reconciler(ReconciliationDecision.CONFIRMED_APPLIED)

    with pytest.raises(EffectError, match="requires outcome_unknown"):
        EffectRuntime(path).reconcile_effect(
            "mission-1", "step-0", "effect-0", reconciler
        )
    assert reconciler.calls == 0


def test_reconciliation_correlation_mismatch_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    runtime.execute_effect(
        "mission-1",
        "step-0",
        "effect-0",
        _Executor(path, EffectExecutionOutcome.UNKNOWN),
    )

    class WrongReconciler:
        def reconcile(self, effect):
            return ReconciliationResult(
                correlation_key="wrong",
                decision=ReconciliationDecision.CONFIRMED_APPLIED,
                evidence={},
            )

    with pytest.raises(EffectContractError, match="does not match"):
        runtime.reconcile_effect(
            "mission-1", "step-0", "effect-0", WrongReconciler()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN


def test_invalid_adapter_result_type_leaves_unknown(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class InvalidExecutor:
        def execute(self, intent):
            return {"outcome": "applied"}

    with pytest.raises(EffectContractError, match="invalid result type"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", InvalidExecutor()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN
