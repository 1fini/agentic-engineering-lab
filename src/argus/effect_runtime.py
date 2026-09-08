"""Generic Phase 3 effect execution and reconciliation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol

from argus.effects import (
    AmbiguousEffectError,
    EffectError,
    EffectIntent,
    EffectReceipt,
    EffectReceiptOutcome,
    EffectReceiptSource,
    EffectRecord,
    EffectState,
    EffectStore,
)


class EffectContractError(EffectError):
    """Raised when an executor/reconciler violates the generic effect contract."""


class EffectExecutionOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"
    UNKNOWN = "unknown"


class ReconciliationDecision(StrEnum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    STILL_UNKNOWN = "still_unknown"


class EffectRuntimeDecision(StrEnum):
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"
    BLOCKED_UNKNOWN = "blocked_unknown"


@dataclass(frozen=True)
class EffectExecutionResult:
    correlation_key: str
    outcome: EffectExecutionOutcome
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.correlation_key:
            raise ValueError("execution result correlation_key must be non-empty")
        from argus.model import canonical_json

        canonical_json(self.evidence)


@dataclass(frozen=True)
class ReconciliationResult:
    correlation_key: str
    decision: ReconciliationDecision
    evidence: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.correlation_key:
            raise ValueError("reconciliation result correlation_key must be non-empty")
        from argus.model import canonical_json

        canonical_json(self.evidence)


@dataclass(frozen=True)
class EffectRuntimeResult:
    decision: EffectRuntimeDecision
    effect: EffectRecord


class EffectExecutor(Protocol):
    def execute(self, intent: EffectIntent) -> EffectExecutionResult: ...


class EffectReconciler(Protocol):
    def reconcile(self, effect: EffectRecord) -> ReconciliationResult: ...


class EffectRuntime:
    """Compose durable effect state with consumer-owned execution/reconciliation."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def execute_effect(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        executor: EffectExecutor,
        *,
        now: str | datetime | None = None,
    ) -> EffectRuntimeResult:
        with EffectStore(self.path) as store:
            current = store.load(mission_id, step_id, effect_id)
            if current.state is EffectState.OUTCOME_UNKNOWN:
                raise AmbiguousEffectError(
                    f"effect {current.intent.correlation_key} has unknown external outcome; reconcile before replay"
                )
            if current.state is not EffectState.INTENT_COMMITTED:
                raise EffectError(
                    f"effect cannot execute from terminal state {current.state.value}"
                )
            pending = store.begin_execution(
                mission_id,
                step_id,
                effect_id,
                recorded_at=now,
            )

        # The durable OUTCOME_UNKNOWN boundary exists before consumer code is called.
        # Any exception/process death from this point leaves fail-closed ambiguity.
        result = executor.execute(pending.intent)
        if not isinstance(result, EffectExecutionResult):
            raise EffectContractError("effect executor returned an invalid result type")
        self._validate_correlation(pending, result.correlation_key, "execution")

        if result.outcome is EffectExecutionOutcome.UNKNOWN:
            with EffectStore(self.path) as store:
                ambiguous = store.load(mission_id, step_id, effect_id)
            return EffectRuntimeResult(
                decision=EffectRuntimeDecision.BLOCKED_UNKNOWN,
                effect=ambiguous,
            )

        receipt = EffectReceipt(
            correlation_key=result.correlation_key,
            outcome=(
                EffectReceiptOutcome.APPLIED
                if result.outcome is EffectExecutionOutcome.APPLIED
                else EffectReceiptOutcome.NOT_APPLIED
            ),
            evidence=result.evidence,
            source=EffectReceiptSource.EXECUTION,
        )
        with EffectStore(self.path) as store:
            confirmed = store.record_receipt(
                mission_id,
                step_id,
                effect_id,
                receipt,
                recorded_at=now,
            )
        return EffectRuntimeResult(
            decision=(
                EffectRuntimeDecision.CONFIRMED_APPLIED
                if confirmed.state is EffectState.CONFIRMED_APPLIED
                else EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
            ),
            effect=confirmed,
        )

    def reconcile_effect(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        reconciler: EffectReconciler,
        *,
        now: str | datetime | None = None,
    ) -> EffectRuntimeResult:
        with EffectStore(self.path) as store:
            current = store.load(mission_id, step_id, effect_id)
        if current.state is not EffectState.OUTCOME_UNKNOWN:
            raise EffectError(
                f"reconciliation requires outcome_unknown state, got {current.state.value}"
            )

        result = reconciler.reconcile(current)
        if not isinstance(result, ReconciliationResult):
            raise EffectContractError("effect reconciler returned an invalid result type")
        self._validate_correlation(current, result.correlation_key, "reconciliation")

        if result.decision is ReconciliationDecision.STILL_UNKNOWN:
            with EffectStore(self.path) as store:
                ambiguous = store.load(mission_id, step_id, effect_id)
            return EffectRuntimeResult(
                decision=EffectRuntimeDecision.BLOCKED_UNKNOWN,
                effect=ambiguous,
            )

        receipt = EffectReceipt(
            correlation_key=result.correlation_key,
            outcome=(
                EffectReceiptOutcome.APPLIED
                if result.decision is ReconciliationDecision.CONFIRMED_APPLIED
                else EffectReceiptOutcome.NOT_APPLIED
            ),
            evidence=result.evidence,
            source=EffectReceiptSource.RECONCILIATION,
        )
        with EffectStore(self.path) as store:
            confirmed = store.record_receipt(
                mission_id,
                step_id,
                effect_id,
                receipt,
                recorded_at=now,
            )
        return EffectRuntimeResult(
            decision=(
                EffectRuntimeDecision.CONFIRMED_APPLIED
                if confirmed.state is EffectState.CONFIRMED_APPLIED
                else EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
            ),
            effect=confirmed,
        )

    @staticmethod
    def _validate_correlation(
        effect: EffectRecord,
        correlation_key: str,
        source: str,
    ) -> None:
        if correlation_key != effect.intent.correlation_key:
            raise EffectContractError(
                f"{source} result correlation_key does not match durable effect identity"
            )
