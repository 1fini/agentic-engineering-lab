from __future__ import annotations

import sqlite3

import pytest

from argus.model import (
    CorruptStateError,
    DuplicateMissionError,
    InvalidTransitionError,
    MissionState,
    StepEnvelope,
    StepResult,
    StepResultKind,
    StepState,
    UnsupportedSchemaVersionError,
)
from argus.store import SqliteMissionStore


def _steps(mission_id: str = "mission-1") -> list[StepEnvelope]:
    return [
        StepEnvelope(
            mission_id=mission_id,
            step_id="step-a",
            ordinal=0,
            operation="fixture.prepare",
            payload={"value": 1, "nested": {"b": 2, "a": 1}},
        ),
        StepEnvelope(
            mission_id=mission_id,
            step_id="step-b",
            ordinal=1,
            operation="fixture.finish",
            payload={"value": 2},
        ),
    ]


def test_create_mission_persists_ordered_steps_and_journal(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path) as store:
        mission = store.create_mission("mission-1", _steps())
        assert mission.state is MissionState.PENDING
        assert [step.envelope.step_id for step in store.list_steps("mission-1")] == [
            "step-a",
            "step-b",
        ]
        journal = store.journal("mission-1")
        assert [entry.event_type for entry in journal] == ["created", "created", "created"]
        assert [entry.step_id for entry in journal] == [None, "step-a", "step-b"]
        assert [entry.sequence for entry in journal] == sorted(
            entry.sequence for entry in journal
        )


def test_create_and_transitions_are_idempotent(tmp_path) -> None:
    path = tmp_path / "argus.db"
    steps = _steps()
    with SqliteMissionStore(path) as store:
        first = store.create_mission("mission-1", steps)
        journal_count = len(store.journal("mission-1"))
        second = store.create_mission("mission-1", steps)
        assert second == first
        assert len(store.journal("mission-1")) == journal_count

        running = store.transition_step("mission-1", "step-a", StepState.RUNNING)
        count_after_running = len(store.journal("mission-1"))
        same = store.transition_step("mission-1", "step-a", StepState.RUNNING)
        assert same == running
        assert len(store.journal("mission-1")) == count_after_running


def test_reusing_mission_id_with_different_definition_fails(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())
        changed = _steps()
        changed[1] = StepEnvelope(
            mission_id="mission-1",
            step_id="step-b",
            ordinal=1,
            operation="fixture.different",
            payload={"value": 2},
        )
        with pytest.raises(DuplicateMissionError):
            store.create_mission("mission-1", changed)


def test_idempotency_key_is_stable_for_canonical_payload() -> None:
    left = StepEnvelope(
        mission_id="mission-1",
        step_id="step-a",
        ordinal=0,
        operation="fixture.prepare",
        payload={"a": 1, "b": {"x": 2, "y": 3}},
    )
    right = StepEnvelope(
        mission_id="mission-1",
        step_id="step-a",
        ordinal=0,
        operation="fixture.prepare",
        payload={"b": {"y": 3, "x": 2}, "a": 1},
    )
    assert left.idempotency_key == right.idempotency_key


def test_step_result_and_state_survive_reopen(tmp_path) -> None:
    path = tmp_path / "argus.db"
    result = StepResult(kind=StepResultKind.SUCCESS, output={"artifact": "abc"})
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())
        store.transition_mission("mission-1", MissionState.RUNNING)
        store.transition_step("mission-1", "step-a", StepState.RUNNING)
        store.transition_step(
            "mission-1", "step-a", StepState.SUCCEEDED, result=result
        )

    with SqliteMissionStore(path) as reopened:
        assert reopened.load_mission("mission-1").state is MissionState.RUNNING
        step = reopened.load_step("mission-1", "step-a")
        assert step.state is StepState.SUCCEEDED
        assert step.result == result
        assert [entry.to_state for entry in reopened.journal("mission-1")][-3:] == [
            MissionState.RUNNING.value,
            StepState.RUNNING.value,
            StepState.SUCCEEDED.value,
        ]


def test_invalid_transitions_fail_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())
        success = StepResult(kind=StepResultKind.SUCCESS, output={})

        with pytest.raises(InvalidTransitionError):
            store.transition_step(
                "mission-1", "step-a", StepState.SUCCEEDED, result=success
            )
        with pytest.raises(InvalidTransitionError):
            store.transition_step(
                "mission-1", "step-a", StepState.FAILED, result=success
            )
        with pytest.raises(InvalidTransitionError):
            store.transition_mission("mission-1", MissionState.COMPLETED)

        assert store.load_step("mission-1", "step-a").state is StepState.PENDING
        assert store.load_mission("mission-1").state is MissionState.PENDING


def test_transaction_rolls_back_state_when_journal_write_fails(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_state_journal
            BEFORE INSERT ON journal
            WHEN NEW.event_type = 'state_transition'
            BEGIN
                SELECT RAISE(ABORT, 'injected journal failure');
            END;
            """
        )

    with SqliteMissionStore(path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.transition_step("mission-1", "step-a", StepState.RUNNING)
        assert store.load_step("mission-1", "step-a").state is StepState.PENDING
        assert len(store.journal("mission-1")) == 3


def test_unsupported_storage_schema_version_fails_on_reopen(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path):
        pass
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE argus_metadata SET value = '999' WHERE key = 'storage_schema_version'"
        )

    with pytest.raises(UnsupportedSchemaVersionError):
        SqliteMissionStore(path)


def test_corrupt_payload_is_reported_instead_of_guessed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE steps SET payload_json = '{not-json' WHERE step_id = 'step-a'"
        )

    with SqliteMissionStore(path) as store:
        with pytest.raises(CorruptStateError):
            store.list_steps("mission-1")


def test_non_sqlite_state_file_fails_safely(tmp_path) -> None:
    path = tmp_path / "argus.db"
    path.write_bytes(b"not a sqlite database")
    with pytest.raises(CorruptStateError):
        SqliteMissionStore(path)


def test_all_steps_must_succeed_before_mission_completion(tmp_path) -> None:
    path = tmp_path / "argus.db"
    success = StepResult(kind=StepResultKind.SUCCESS, output={})
    with SqliteMissionStore(path) as store:
        store.create_mission("mission-1", _steps())
        store.transition_mission("mission-1", MissionState.RUNNING)
        for step_id in ("step-a", "step-b"):
            store.transition_step("mission-1", step_id, StepState.RUNNING)
            store.transition_step(
                "mission-1", step_id, StepState.SUCCEEDED, result=success
            )
        completed = store.transition_mission("mission-1", MissionState.COMPLETED)
        assert completed.state is MissionState.COMPLETED
