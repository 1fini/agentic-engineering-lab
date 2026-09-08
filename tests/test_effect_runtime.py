from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from argus.effect_attempts import (
    EffectAttemptLimitError,
    EffectAttemptState,
    EffectAttemptStore,
    EffectReplayPolicy,
)
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
    outcomes: list[EffectExecutionOutcome]
    calls: int = 0
    attempt_keys: list[str] = field(default_factory=list)

    def execute(self, context):
        self.calls += 1
        self.attempt_keys.append(context.attempt.attempt_key)
        with EffectStore(self.path) as store:
            durable = store.load(
                context.effect.intent.mission_id,
                context.effect.intent.step_id,
                context.effect.intent.effect_id,
            )
            assert durable.state is EffectState.OUTCOME_UNKNOWN
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        return EffectExecutionResult(
            correlation_key=context.effect.intent.correlation_key,
            attempt_key=context.attempt.attempt_key,
            outcome=outcome,
            evidence={"remote_ref": f"remote://opaque-{self.calls}"},
        )


@dataclass
class _Reconciler:
    decision: ReconciliationDecision
    calls: int = 0

    def reconcile(self, context):
        self.calls += 1
        assert context.effect.state is EffectState.OUTCOME_UNKNOWN
        assert context.attempt.state is EffectAttemptState.OUTCOME_UNKNOWN
        return ReconciliationResult(
            correlation_key=context.effect.intent.correlation_key,
            attempt_key=context.attempt.attempt_key,
            decision=self.decision,
            evidence={"lookup_ref": "evidence://opaque"},
        )


def test_executor_is_called_only_after_unknown_boundary_and_attempt_are_durable(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, [EffectExecutionOutcome.APPLIED])

    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", executor
    )

    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert result.effect.state is EffectState.CONFIRMED_APPLIED
    assert result.effect.receipt is not None
    assert result.effect.receipt.source is EffectReceiptSource.EXECUTION
    assert result.attempt.attempt_no == 1
    assert result.attempt.state is EffectAttemptState.CONFIRMED_APPLIED
    assert executor.attempt_keys == [result.attempt.attempt_key]


def test_executor_not_applied_is_confirmed_without_automatic_retry(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, [EffectExecutionOutcome.NOT_APPLIED])

    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", executor
    )

    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
    assert result.effect.state is EffectState.CONFIRMED_NOT_APPLIED
    assert result.attempt.state is EffectAttemptState.CONFIRMED_NOT_APPLIED


def test_safe_reattempt_requires_confirmed_not_applied_and_gets_new_attempt_key(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(
        path,
        [EffectExecutionOutcome.NOT_APPLIED, EffectExecutionOutcome.APPLIED],
    )

    first = runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    second = runtime.reattempt_effect(
        "mission-1",
        "step-0",
        "effect-0",
        executor,
        policy=EffectReplayPolicy(max_effect_attempts=2),
    )

    assert first.attempt.attempt_no == 1
    assert second.attempt.attempt_no == 2
    assert first.attempt.attempt_key != second.attempt.attempt_key
    assert second.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert executor.calls == 2
    with EffectAttemptStore(path) as attempts:
        history = attempts.list_attempts("mission-1", "step-0", "effect-0")
    assert [item.state for item in history] == [
        EffectAttemptState.CONFIRMED_NOT_APPLIED,
        EffectAttemptState.CONFIRMED_APPLIED,
    ]


def test_attempt_limit_blocks_second_external_call(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, [EffectExecutionOutcome.NOT_APPLIED, EffectExecutionOutcome.APPLIED])
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    with pytest.raises(EffectAttemptLimitError, match="limit exhausted"):
        runtime.reattempt_effect(
            "mission-1",
            "step-0",
            "effect-0",
            executor,
            policy=EffectReplayPolicy(max_effect_attempts=1),
        )
    assert executor.calls == 1


def test_applied_effect_can_never_be_reattempted(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, [EffectExecutionOutcome.APPLIED])
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    with pytest.raises(EffectError, match="never be re-attempted"):
        runtime.reattempt_effect("mission-1", "step-0", "effect-0", executor)
    assert executor.calls == 1


def test_executor_unknown_leaves_effect_blocked_and_second_execute_is_forbidden(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    executor = _Executor(path, [EffectExecutionOutcome.UNKNOWN])
    runtime = EffectRuntime(path)

    result = runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    assert result.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert result.effect.state is EffectState.OUTCOME_UNKNOWN
    assert result.attempt.state is EffectAttemptState.OUTCOME_UNKNOWN
    assert executor.calls == 1

    with pytest.raises(AmbiguousEffectError, match="reconcile before replay"):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    with pytest.raises(AmbiguousEffectError):
        runtime.reattempt_effect("mission-1", "step-0", "effect-0", executor)
    assert executor.calls == 1


def test_executor_exception_leaves_durable_unknown_attempt_and_blocks_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class ExplodingExecutor:
        calls = 0

        def execute(self, context):
            self.calls += 1
            raise RuntimeError("external boundary crashed")

    executor = ExplodingExecutor()
    runtime = EffectRuntime(path)
    with pytest.raises(RuntimeError, match="external boundary crashed"):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN
    with EffectAttemptStore(path) as attempts:
        latest = attempts.latest_attempt("mission-1", "step-0", "effect-0")
        assert latest is not None
        assert latest.state is EffectAttemptState.OUTCOME_UNKNOWN
    with pytest.raises(AmbiguousEffectError):
        runtime.execute_effect("mission-1", "step-0", "effect-0", executor)
    assert executor.calls == 1


def test_execution_identity_mismatch_fails_closed_in_unknown_state(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class WrongIdentity:
        def execute(self, context):
            return EffectExecutionResult(
                correlation_key=context.effect.intent.correlation_key,
                attempt_key="wrong-attempt",
                outcome=EffectExecutionOutcome.APPLIED,
                evidence={},
            )

    with pytest.raises(EffectContractError, match="attempt_key"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", WrongIdentity()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN


def test_reconciliation_applied_commits_receipt_without_executor_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, [EffectExecutionOutcome.UNKNOWN])
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
    assert result.attempt.state is EffectAttemptState.CONFIRMED_APPLIED


def test_reconciliation_not_applied_then_explicit_reattempt_is_allowed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, [EffectExecutionOutcome.UNKNOWN, EffectExecutionOutcome.APPLIED])
    runtime.execute_effect("mission-1", "step-0", "effect-0", executor)

    result = runtime.reconcile_effect(
        "mission-1",
        "step-0",
        "effect-0",
        _Reconciler(ReconciliationDecision.CONFIRMED_NOT_APPLIED),
    )
    assert executor.calls == 1
    assert result.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED

    retried = runtime.reattempt_effect("mission-1", "step-0", "effect-0", executor)
    assert retried.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert executor.calls == 2


def test_still_unknown_reconciliation_can_repeat_without_effect_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    executor = _Executor(path, [EffectExecutionOutcome.UNKNOWN])
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


def test_reconciliation_attempt_identity_mismatch_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    runtime = EffectRuntime(path)
    runtime.execute_effect(
        "mission-1",
        "step-0",
        "effect-0",
        _Executor(path, [EffectExecutionOutcome.UNKNOWN]),
    )

    class WrongReconciler:
        def reconcile(self, context):
            return ReconciliationResult(
                correlation_key=context.effect.intent.correlation_key,
                attempt_key="wrong",
                decision=ReconciliationDecision.CONFIRMED_APPLIED,
                evidence={},
            )

    with pytest.raises(EffectContractError, match="attempt_key"):
        runtime.reconcile_effect(
            "mission-1", "step-0", "effect-0", WrongReconciler()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN


def test_invalid_adapter_result_type_leaves_unknown(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    class InvalidExecutor:
        def execute(self, context):
            return {"outcome": "applied"}

    with pytest.raises(EffectContractError, match="invalid result type"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", InvalidExecutor()
        )
    with EffectStore(path) as store:
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN
