from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from argus.effects import (
    AmbiguousEffectError,
    DuplicateEffectError,
    EffectError,
    EffectIntent,
    EffectReceipt,
    EffectReceiptOutcome,
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
    return EffectIntent(
        mission_id="mission-1",
        step_id="step-0",
        effect_id="effect-0",
        operation="external.apply",
        payload={
            "artifact_ref": "artifact://candidate",
            "policy_ref": "policy://v1",
        },
    )


def test_intent_is_durable_idempotent_and_stable_across_restart(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    with EffectStore(path) as store:
        first = store.create_intent(intent, recorded_at=created_at)
        duplicate = store.create_intent(intent, recorded_at=created_at + timedelta(seconds=1))
        assert first == duplicate
        assert first.state is EffectState.INTENT_COMMITTED
        assert first.receipt is None
        assert first.intent.correlation_key.startswith("argus:effect:v1:")
        assert len(store.history("mission-1")) == 1

    with EffectStore(path) as store:
        reopened = store.load("mission-1", "step-0", "effect-0")
        assert reopened == first
        assert reopened.intent.correlation_key == first.intent.correlation_key


def test_conflicting_effect_identity_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        store.create_intent(intent)
        conflicting = EffectIntent(
            mission_id=intent.mission_id,
            step_id=intent.step_id,
            effect_id=intent.effect_id,
            operation=intent.operation,
            payload={"artifact_ref": "artifact://different"},
        )
        with pytest.raises(DuplicateEffectError):
            store.create_intent(conflicting)


def test_begin_execution_enters_unknown_boundary_before_external_call(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    t0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    with EffectStore(path) as store:
        store.create_intent(intent, recorded_at=t0)
        record = store.begin_execution(
            "mission-1", "step-0", "effect-0", recorded_at=t0 + timedelta(seconds=1)
        )
        assert record.state is EffectState.OUTCOME_UNKNOWN
        assert record.receipt is None

        with pytest.raises(AmbiguousEffectError, match="unknown external outcome"):
            store.begin_execution(
                "mission-1",
                "step-0",
                "effect-0",
                recorded_at=t0 + timedelta(seconds=2),
            )

    with EffectStore(path) as store:
        reopened = store.load("mission-1", "step-0", "effect-0")
        assert reopened.state is EffectState.OUTCOME_UNKNOWN
        with pytest.raises(AmbiguousEffectError):
            store.begin_execution("mission-1", "step-0", "effect-0")


def test_receipt_confirms_applied_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        created = store.create_intent(intent)
        store.begin_execution("mission-1", "step-0", "effect-0")
        receipt = EffectReceipt(
            correlation_key=created.intent.correlation_key or "",
            outcome=EffectReceiptOutcome.APPLIED,
            source=EffectReceiptSource.EXECUTION,
            evidence={"remote_ref": "remote://opaque-123"},
        )
        applied = store.record_receipt("mission-1", "step-0", "effect-0", receipt)
        assert applied.state is EffectState.CONFIRMED_APPLIED
        assert applied.receipt == receipt

        duplicate = store.record_receipt("mission-1", "step-0", "effect-0", receipt)
        assert duplicate == applied
        assert [event.event_type for event in store.history("mission-1")] == [
            "intent_committed",
            "execution_boundary_entered",
            "receipt_committed",
        ]


def test_not_applied_receipt_is_distinct_from_unknown(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        created = store.create_intent(intent)
        store.begin_execution("mission-1", "step-0", "effect-0")
        receipt = EffectReceipt(
            correlation_key=created.intent.correlation_key or "",
            outcome=EffectReceiptOutcome.NOT_APPLIED,
            evidence={"reason_ref": "evidence://not-applied"},
        )
        record = store.record_receipt("mission-1", "step-0", "effect-0", receipt)
        assert record.state is EffectState.CONFIRMED_NOT_APPLIED
        assert record.receipt is not None
        assert record.receipt.outcome is EffectReceiptOutcome.NOT_APPLIED


def test_receipt_requires_unknown_boundary_and_matching_correlation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        created = store.create_intent(intent)
        receipt = EffectReceipt(
            correlation_key=created.intent.correlation_key or "",
            outcome=EffectReceiptOutcome.APPLIED,
            evidence={},
        )
        with pytest.raises(EffectError, match="requires outcome_unknown"):
            store.record_receipt("mission-1", "step-0", "effect-0", receipt)

        store.begin_execution("mission-1", "step-0", "effect-0")
        wrong = EffectReceipt(
            correlation_key="argus:effect:v1:" + "0" * 64,
            outcome=EffectReceiptOutcome.APPLIED,
            evidence={},
        )
        with pytest.raises(EffectError, match="does not match"):
            store.record_receipt("mission-1", "step-0", "effect-0", wrong)


def test_terminal_receipt_cannot_be_replaced(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        created = store.create_intent(intent)
        store.begin_execution("mission-1", "step-0", "effect-0")
        applied = EffectReceipt(
            correlation_key=created.intent.correlation_key or "",
            outcome=EffectReceiptOutcome.APPLIED,
            evidence={"remote_ref": "remote://one"},
        )
        store.record_receipt("mission-1", "step-0", "effect-0", applied)
        conflicting = EffectReceipt(
            correlation_key=created.intent.correlation_key or "",
            outcome=EffectReceiptOutcome.NOT_APPLIED,
            evidence={"remote_ref": "remote://two"},
        )
        with pytest.raises(EffectError, match="different terminal receipt"):
            store.record_receipt("mission-1", "step-0", "effect-0", conflicting)


def test_effect_transition_and_event_are_transactional(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        store.create_intent(intent)

    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TRIGGER abort_effect_transition
        BEFORE INSERT ON effect_events
        WHEN NEW.event_type = 'execution_boundary_entered'
        BEGIN
            SELECT RAISE(ABORT, 'injected effect event failure');
        END;
        """
    )
    connection.commit()
    connection.close()

    with EffectStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.begin_execution("mission-1", "step-0", "effect-0")
        assert store.load("mission-1", "step-0", "effect-0").state is EffectState.INTENT_COMMITTED
        assert [event.event_type for event in store.history("mission-1")] == [
            "intent_committed"
        ]


def test_unknown_step_and_uninitialized_store_fail_visibly(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    bad = EffectIntent(
        mission_id="mission-1",
        step_id="missing",
        effect_id="effect-x",
        operation="external.apply",
        payload={},
    )
    with EffectStore(path) as store:
        with pytest.raises(KeyError, match="unknown step"):
            store.create_intent(bad)

    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()
    with pytest.raises(EffectError, match="requires initialized"):
        EffectStore(empty)


def test_unsupported_effect_schema_and_corrupt_payload_fail_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    with EffectStore(path) as store:
        store.create_intent(intent)

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE argus_effect_metadata SET value = '99' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(EffectError, match="unsupported effect schema version"):
        EffectStore(path)

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE argus_effect_metadata SET value = '1' WHERE key = 'schema_version'"
    )
    connection.execute("UPDATE effects SET payload_json = '{broken' WHERE effect_id = 'effect-0'")
    connection.commit()
    connection.close()
    with EffectStore(path) as store:
        with pytest.raises(EffectError, match="payload is corrupt"):
            store.load("mission-1", "step-0", "effect-0")


def test_fixture_contract_is_domain_opaque(tmp_path) -> None:
    path = tmp_path / "argus.db"
    intent = _prepare(path)
    forbidden = ("youtube", "short", "retention", "editorial", "hook", "publication")
    material = repr(intent).lower()
    assert not any(term in material for term in forbidden)
