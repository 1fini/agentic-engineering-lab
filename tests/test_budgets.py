from __future__ import annotations

from decimal import Decimal
import sqlite3

import pytest

from argus.attempts import AttemptStore
from argus.budgets import (
    BudgetDecisionKind,
    BudgetError,
    BudgetExhaustedError,
    BudgetPolicy,
    BudgetReason,
    BudgetStore,
    ReservationState,
)
from argus.effect_attempts import EffectAttemptStore
from argus.effects import EffectIntent, EffectStore
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _mission(path, mission_id="mission-1") -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            mission_id,
            [
                StepEnvelope(
                    mission_id=mission_id,
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.compute",
                    payload={"input_ref": "artifact://opaque"},
                )
            ],
        )


def test_budget_policy_is_durable_and_monotonic(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    first = BudgetPolicy(
        mission_id="mission-1",
        policy_version=1,
        worker_attempt_limit=3,
        effect_attempt_limit=2,
        spend_limit_usd="10.50",
    )
    with BudgetStore(path) as budgets:
        assert budgets.set_policy(first) == first
        assert budgets.set_policy(first) == first
        assert len(budgets.history("mission-1")) == 1
        updated = budgets.set_policy(
            BudgetPolicy(
                mission_id="mission-1",
                policy_version=2,
                worker_attempt_limit=4,
                effect_attempt_limit=3,
                spend_limit_usd="12",
            )
        )
        assert updated.policy_version == 2
        with pytest.raises(BudgetError, match="strictly increasing"):
            budgets.set_policy(
                BudgetPolicy(
                    mission_id="mission-1",
                    policy_version=1,
                    worker_attempt_limit=9,
                )
            )

    with BudgetStore(path) as reopened:
        assert reopened.load_policy("mission-1").policy_hash == updated.policy_hash


def test_worker_attempt_budget_counts_authoritative_attempt_rows(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", worker_attempt_limit=1)
        )
        before = budgets.check_worker_attempt("mission-1")
        assert before.kind is BudgetDecisionKind.ALLOW
        assert before.used == 0

    with AttemptStore(path) as attempts:
        attempts.begin_attempt(
            "mission-1",
            "step-0",
            started_at="2026-09-08T13:00:00Z",
        )

    with BudgetStore(path) as budgets:
        after = budgets.check_worker_attempt("mission-1")
        assert after.kind is BudgetDecisionKind.DENY
        assert after.reason is BudgetReason.WORKER_ATTEMPT_EXHAUSTED
        assert after.used == 1
        assert after.limit == 1


def test_effect_attempt_budget_counts_phase3_attempt_lineage(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", effect_attempt_limit=1)
        )
        assert budgets.check_effect_attempt("mission-1").kind is BudgetDecisionKind.ALLOW

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
        effects.begin_execution("mission-1", "step-0", "effect-0")
    with EffectAttemptStore(path) as effect_attempts:
        attempts = effect_attempts.list_attempts("mission-1", "step-0", "effect-0")
        assert len(attempts) == 1

    with BudgetStore(path) as budgets:
        decision = budgets.check_effect_attempt("mission-1")
        assert decision.kind is BudgetDecisionKind.DENY
        assert decision.reason is BudgetReason.EFFECT_ATTEMPT_EXHAUSTED
        assert decision.used == 1


def test_spend_reservation_counts_against_available_budget_across_restart(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", spend_limit_usd="10")
        )
        reservation = budgets.reserve_spend("mission-1", "call-1", "6")
        assert reservation.state is ReservationState.RESERVED
        assert reservation.reserved_usd == Decimal("6")
        snapshot = budgets.snapshot("mission-1")
        assert snapshot.spend_reserved_usd == Decimal("6")
        assert snapshot.spend_available_usd == Decimal("4")
        denied = budgets.check_spend("mission-1", "5")
        assert denied.kind is BudgetDecisionKind.DENY
        assert denied.reason is BudgetReason.SPEND_EXHAUSTED

    with BudgetStore(path) as reopened:
        snapshot = reopened.snapshot("mission-1")
        assert snapshot.spend_reserved_usd == Decimal("6")
        assert snapshot.spend_available_usd == Decimal("4")
        duplicate = reopened.reserve_spend("mission-1", "call-1", "6")
        assert duplicate.state is ReservationState.RESERVED
        assert len(reopened.list_reservations("mission-1")) == 1


def test_commit_uses_actual_spend_and_releases_unused_reservation_capacity(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", spend_limit_usd="10")
        )
        budgets.reserve_spend("mission-1", "call-1", "6")
        committed = budgets.commit_spend("call-1", "4")
        assert committed.state is ReservationState.COMMITTED
        assert committed.committed_usd == Decimal("4")
        snapshot = budgets.snapshot("mission-1")
        assert snapshot.spend_committed_usd == Decimal("4")
        assert snapshot.spend_reserved_usd == 0
        assert snapshot.spend_available_usd == Decimal("6")
        assert budgets.commit_spend("call-1", "4") == committed
        with pytest.raises(BudgetError, match="cannot change"):
            budgets.commit_spend("call-1", "3")


def test_release_returns_reserved_capacity_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", spend_limit_usd="10")
        )
        budgets.reserve_spend("mission-1", "call-1", "7")
        released = budgets.release_spend("call-1")
        assert released.state is ReservationState.RELEASED
        assert budgets.release_spend("call-1") == released
        assert budgets.snapshot("mission-1").spend_available_usd == Decimal("10")
        with pytest.raises(BudgetError, match="cannot be committed"):
            budgets.commit_spend("call-1", "1")


def test_actual_spend_cannot_exceed_reservation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", spend_limit_usd="10")
        )
        budgets.reserve_spend("mission-1", "call-1", "3")
        with pytest.raises(BudgetError, match="cannot exceed"):
            budgets.commit_spend("call-1", "4")
        assert budgets.load_reservation("call-1").state is ReservationState.RESERVED


def test_reservation_fails_before_write_when_budget_is_exhausted(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(mission_id="mission-1", spend_limit_usd="5")
        )
        budgets.reserve_spend("mission-1", "call-1", "5")
        with pytest.raises(BudgetExhaustedError, match="exhausted"):
            budgets.reserve_spend("mission-1", "call-2", "0.01")
        assert budgets.get_reservation("call-2") is None


def test_budget_checks_do_not_append_events_or_consume_budget(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(
                mission_id="mission-1",
                worker_attempt_limit=1,
                effect_attempt_limit=1,
                spend_limit_usd="2",
            )
        )
        baseline = len(budgets.history("mission-1"))
        for _ in range(3):
            assert budgets.check_worker_attempt("mission-1").kind is BudgetDecisionKind.ALLOW
            assert budgets.check_effect_attempt("mission-1").kind is BudgetDecisionKind.ALLOW
            assert budgets.check_spend("mission-1", "1").kind is BudgetDecisionKind.ALLOW
        assert len(budgets.history("mission-1")) == baseline
        snapshot = budgets.snapshot("mission-1")
        assert snapshot.worker_attempts_used == 0
        assert snapshot.effect_attempts_used == 0
        assert snapshot.spend_committed_usd == 0
        assert snapshot.spend_reserved_usd == 0


def test_corrupt_budget_policy_hash_and_schema_fail_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with BudgetStore(path) as budgets:
        budgets.set_policy(BudgetPolicy(mission_id="mission-1", spend_limit_usd="2"))

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE budget_policies SET policy_hash = 'broken' WHERE mission_id = 'mission-1'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(BudgetError, match="policy hash mismatch"):
        BudgetStore(path)

    path2 = tmp_path / "argus2.db"
    _mission(path2)
    with BudgetStore(path2) as budgets:
        budgets.set_policy(BudgetPolicy(mission_id="mission-1"))
    connection = sqlite3.connect(path2)
    connection.execute(
        "UPDATE argus_budget_metadata SET value = '99' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(BudgetError, match="unsupported budget schema version"):
        BudgetStore(path2)


def test_budget_reference_values_are_domain_opaque() -> None:
    values = "mission-1 fixture.compute artifact://opaque workload-alpha"
    forbidden = ("youtube", "short", "retention", "editorial", "publication")
    for term in forbidden:
        assert term not in values.lower()
