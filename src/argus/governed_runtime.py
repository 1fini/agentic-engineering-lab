"""Phase 4 governed execution gates for workers and external effects.

`GovernedRuntime` is the authoritative unattended execution surface once a mission
opts into Phase 4 policies. It composes existing Phase 2/3 runtimes rather than
replacing their recovery semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
import sqlite3
from typing import Callable

from argus.attempts import (
    AmbiguousAttemptError,
    AttemptDecision,
    AttemptPolicy,
    AttemptState,
    AttemptStore,
    WorkerInvoker,
)
from argus.budgets import (
    BudgetDecisionKind,
    BudgetExhaustedError,
    BudgetPolicy,
    BudgetStore,
    SpendReservation,
)
from argus.effect_attempts import EffectAttemptStore, EffectReplayPolicy
from argus.effect_runtime import EffectExecutor, EffectRuntime, EffectRuntimeResult
from argus.effects import AmbiguousEffectError, EffectState, EffectStore
from argus.guardrails import GateDecision, GuardrailStore
from argus.model import ArgusStateError
from argus.phase2_runtime import Phase2Runtime
from argus.scheduler import canonical_due_at
from argus.worker_adapter import InvocationOutcome

_GOVERNED_SCHEMA_VERSION = 1
Money = Decimal | str | int | float
EffectCostResolver = Callable[[EffectRuntimeResult], Money | None]


class GovernedExecutionError(ArgusStateError):
    """Base error for Phase 4 governed execution."""


class ExecutionGateDenied(GovernedExecutionError):
    """Raised before attempt allocation when a durable Phase 4 gate denies work."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"execution gate denied: {reason}")


class ExecutionKind(StrEnum):
    WORKER = "worker"
    EFFECT = "effect"


@dataclass(frozen=True)
class ExecutionAuthorization:
    mission_id: str
    kind: ExecutionKind
    guardrail_policy_hash: str
    budget_policy_hash: str
    reservation: SpendReservation | None


@dataclass(frozen=True)
class ExecutionGateEvent:
    sequence: int
    mission_id: str
    kind: ExecutionKind
    decision: str
    reason: str
    guardrail_policy_hash: str
    budget_policy_hash: str
    reservation_key: str | None
    recorded_at: str


class GovernedRuntime:
    """Compose Phase 4 controls/budgets with Phase 2 worker and Phase 3 effect paths."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize_or_validate()

    def execute_due_worker(
        self,
        mission_id: str,
        step_id: str,
        worker: WorkerInvoker,
        *,
        attempt_policy: AttemptPolicy = AttemptPolicy(),
        now: str | datetime | None = None,
        reserve_spend_usd: Money | None = None,
    ) -> AttemptDecision:
        instant = canonical_due_at(now or datetime.now(timezone.utc))

        due = {
            (item.mission_id, item.step_id)
            for item in Phase2Runtime(self.path).due(instant)
        }
        if (mission_id, step_id) not in due:
            from argus.attempts import AttemptNotDueError

            raise AttemptNotDueError(
                f"step {mission_id}/{step_id} is not durably eligible at {instant}"
            )

        # Resolve Phase 2 ambiguity before allocating any new spend reservation.
        with AttemptStore(self.path) as attempts:
            latest = attempts.latest_attempt(mission_id, step_id)
            if latest is not None and latest.state is AttemptState.STARTED:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has no durable outcome; replay is blocked"
                )
            if latest is not None and latest.outcome is InvocationOutcome.TERMINATION_UNCERTAIN:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has uncertain termination; replay is blocked"
                )

        authorization = self._authorize(
            mission_id,
            ExecutionKind.WORKER,
            subject_key=step_id,
            reserve_spend_usd=reserve_spend_usd,
        )

        decision = Phase2Runtime(self.path).execute_due_step(
            mission_id,
            step_id,
            worker,
            policy=attempt_policy,
            now=instant,
        )
        self._settle_worker_reservation(authorization, decision)
        return decision

    def execute_effect(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        executor: EffectExecutor,
        *,
        now: str | datetime | None = None,
        reserve_spend_usd: Money | None = None,
        cost_resolver: EffectCostResolver | None = None,
    ) -> EffectRuntimeResult:
        with EffectStore(self.path) as effects:
            current = effects.load(mission_id, step_id, effect_id)
        if current.state is EffectState.OUTCOME_UNKNOWN:
            raise AmbiguousEffectError(
                f"effect {current.intent.correlation_key} has unknown external outcome; reconcile before replay"
            )
        if current.state is not EffectState.INTENT_COMMITTED:
            from argus.effects import EffectError

            raise EffectError(
                f"effect cannot execute from terminal state {current.state.value}"
            )

        authorization = self._authorize(
            mission_id,
            ExecutionKind.EFFECT,
            subject_key=f"{step_id}/{effect_id}",
            reserve_spend_usd=reserve_spend_usd,
        )
        result = EffectRuntime(self.path).execute_effect(
            mission_id,
            step_id,
            effect_id,
            executor,
            now=now,
        )
        self._settle_effect_reservation(authorization, result, cost_resolver)
        return result

    def reattempt_effect(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        executor: EffectExecutor,
        *,
        replay_policy: EffectReplayPolicy = EffectReplayPolicy(),
        now: str | datetime | None = None,
        reserve_spend_usd: Money | None = None,
        cost_resolver: EffectCostResolver | None = None,
    ) -> EffectRuntimeResult:
        with EffectStore(self.path) as effects:
            current = effects.load(mission_id, step_id, effect_id)
        if current.state is EffectState.OUTCOME_UNKNOWN:
            raise AmbiguousEffectError(
                f"effect {current.intent.correlation_key} is still ambiguous"
            )
        if current.state is not EffectState.CONFIRMED_NOT_APPLIED:
            from argus.effects import EffectError

            raise EffectError(
                f"re-attempt requires confirmed_not_applied, got {current.state.value}"
            )

        authorization = self._authorize(
            mission_id,
            ExecutionKind.EFFECT,
            subject_key=f"{step_id}/{effect_id}",
            reserve_spend_usd=reserve_spend_usd,
        )
        result = EffectRuntime(self.path).reattempt_effect(
            mission_id,
            step_id,
            effect_id,
            executor,
            policy=replay_policy,
            now=now,
        )
        self._settle_effect_reservation(authorization, result, cost_resolver)
        return result

    def events(self, mission_id: str) -> list[ExecutionGateEvent]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM governed_execution_events WHERE mission_id = ? ORDER BY sequence ASC",
                (mission_id,),
            ).fetchall()
            return [
                ExecutionGateEvent(
                    sequence=int(row["sequence"]),
                    mission_id=row["mission_id"],
                    kind=ExecutionKind(row["kind"]),
                    decision=row["decision"],
                    reason=row["reason"],
                    guardrail_policy_hash=row["guardrail_policy_hash"],
                    budget_policy_hash=row["budget_policy_hash"],
                    reservation_key=row["reservation_key"],
                    recorded_at=row["recorded_at"],
                )
                for row in rows
            ]
        finally:
            connection.close()

    def _authorize(
        self,
        mission_id: str,
        kind: ExecutionKind,
        *,
        subject_key: str,
        reserve_spend_usd: Money | None,
    ) -> ExecutionAuthorization:
        with GuardrailStore(self.path) as guardrails:
            guardrail_record = guardrails.load(mission_id)
            gate = guardrails.check_gate(mission_id)

        with BudgetStore(self.path) as budgets:
            budget_policy = budgets.load_policy(mission_id)

            if gate.decision is GateDecision.DENY:
                self._deny(
                    mission_id,
                    kind,
                    gate.reason.value,
                    guardrail_record.policy.policy_hash,
                    budget_policy.policy_hash,
                )

            attempt_decision = (
                budgets.check_worker_attempt(mission_id)
                if kind is ExecutionKind.WORKER
                else budgets.check_effect_attempt(mission_id)
            )
            if attempt_decision.kind is BudgetDecisionKind.DENY:
                self._deny(
                    mission_id,
                    kind,
                    attempt_decision.reason.value,
                    guardrail_record.policy.policy_hash,
                    budget_policy.policy_hash,
                )

            reservation: SpendReservation | None = None
            if budget_policy.spend_limit_usd is not None:
                if reserve_spend_usd is None:
                    self._deny(
                        mission_id,
                        kind,
                        "spend_reservation_required",
                        guardrail_record.policy.policy_hash,
                        budget_policy.policy_hash,
                    )

                # Derive the next call identity from authoritative attempt evidence.
                # Crucially, call reserve_spend directly: it resolves an existing
                # same-key reservation idempotently *before* testing fresh capacity.
                # This is what makes crash-after-reservation restart safe.
                snapshot = budgets.snapshot(mission_id)
                next_no = (
                    snapshot.worker_attempts_used + 1
                    if kind is ExecutionKind.WORKER
                    else snapshot.effect_attempts_used + 1
                )
                reservation_key = (
                    f"argus:spend:{kind.value}:{mission_id}:{subject_key}:attempt:{next_no}"
                )
                try:
                    reservation = budgets.reserve_spend(
                        mission_id,
                        reservation_key,
                        reserve_spend_usd,
                    )
                except BudgetExhaustedError:
                    self._deny(
                        mission_id,
                        kind,
                        "spend_exhausted",
                        guardrail_record.policy.policy_hash,
                        budget_policy.policy_hash,
                    )

        self._record_gate(
            mission_id,
            kind,
            "allow",
            "allowed",
            guardrail_record.policy.policy_hash,
            budget_policy.policy_hash,
            reservation.reservation_key if reservation else None,
        )
        return ExecutionAuthorization(
            mission_id=mission_id,
            kind=kind,
            guardrail_policy_hash=guardrail_record.policy.policy_hash,
            budget_policy_hash=budget_policy.policy_hash,
            reservation=reservation,
        )

    def _deny(
        self,
        mission_id: str,
        kind: ExecutionKind,
        reason: str,
        guardrail_policy_hash: str,
        budget_policy_hash: str,
    ) -> None:
        self._record_gate(
            mission_id,
            kind,
            "deny",
            reason,
            guardrail_policy_hash,
            budget_policy_hash,
            None,
        )
        raise ExecutionGateDenied(reason)

    def _settle_worker_reservation(
        self,
        authorization: ExecutionAuthorization,
        decision: AttemptDecision,
    ) -> None:
        reservation = authorization.reservation
        if reservation is None:
            return
        actual = decision.attempt.cost_usd
        if actual is None:
            return
        with BudgetStore(self.path) as budgets:
            budgets.commit_spend(reservation.reservation_key, actual)

    def _settle_effect_reservation(
        self,
        authorization: ExecutionAuthorization,
        result: EffectRuntimeResult,
        cost_resolver: EffectCostResolver | None,
    ) -> None:
        reservation = authorization.reservation
        if reservation is None or cost_resolver is None:
            return
        actual = cost_resolver(result)
        if actual is None:
            return
        with BudgetStore(self.path) as budgets:
            budgets.commit_spend(reservation.reservation_key, actual)

    def _record_gate(
        self,
        mission_id: str,
        kind: ExecutionKind,
        decision: str,
        reason: str,
        guardrail_policy_hash: str,
        budget_policy_hash: str,
        reservation_key: str | None,
    ) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO governed_execution_events(
                        mission_id, kind, decision, reason,
                        guardrail_policy_hash, budget_policy_hash,
                        reservation_key, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mission_id,
                        kind.value,
                        decision,
                        reason,
                        guardrail_policy_hash,
                        budget_policy_hash,
                        reservation_key,
                        canonical_due_at(datetime.now(timezone.utc)),
                    ),
                )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def _initialize_or_validate(self) -> None:
        connection = self._connect()
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required = {"missions", "steps", "mission_guardrails", "budget_policies"}
            if not required.issubset(tables):
                raise GovernedExecutionError(
                    "GovernedRuntime requires initialized mission, guardrail, and budget stores"
                )
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS argus_governed_metadata(
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS governed_execution_events(
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        mission_id TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        decision TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        guardrail_policy_hash TEXT NOT NULL,
                        budget_policy_hash TEXT NOT NULL,
                        reservation_key TEXT,
                        recorded_at TEXT NOT NULL,
                        FOREIGN KEY(mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
                    )
                    """
                )
                row = connection.execute(
                    "SELECT value FROM argus_governed_metadata WHERE key = 'schema_version'"
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO argus_governed_metadata(key, value) VALUES ('schema_version', ?)",
                        (str(_GOVERNED_SCHEMA_VERSION),),
                    )
                else:
                    try:
                        version = int(row[0])
                    except (TypeError, ValueError) as exc:
                        raise GovernedExecutionError(
                            "governed runtime schema version is invalid"
                        ) from exc
                    if version != _GOVERNED_SCHEMA_VERSION:
                        raise GovernedExecutionError(
                            f"unsupported governed runtime schema version: {version}"
                        )
        finally:
            connection.close()


__all__ = [
    "EffectCostResolver",
    "ExecutionAuthorization",
    "ExecutionGateDenied",
    "ExecutionGateEvent",
    "ExecutionKind",
    "GovernedExecutionError",
    "GovernedRuntime",
]
