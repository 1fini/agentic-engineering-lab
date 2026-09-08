"""SQLite-backed durable mission and step journal for ARGUS Phase 1."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping, Sequence

from argus.model import (
    CURRENT_SCHEMA_VERSION,
    CorruptStateError,
    DuplicateMissionError,
    InvalidTransitionError,
    JournalEntry,
    MissionRecord,
    MissionState,
    StepEnvelope,
    StepRecord,
    StepResult,
    StepResultKind,
    StepState,
    UnsupportedSchemaVersionError,
    canonical_json,
    derive_idempotency_key,
)

_STORAGE_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
_REQUIRED_TABLES = {"argus_metadata", "missions", "steps", "journal"}

_MISSION_TRANSITIONS: dict[MissionState, set[MissionState]] = {
    MissionState.PENDING: {MissionState.RUNNING, MissionState.FAILED},
    MissionState.RUNNING: {MissionState.COMPLETED, MissionState.FAILED},
    MissionState.COMPLETED: set(),
    MissionState.FAILED: set(),
}

_STEP_TRANSITIONS: dict[StepState, set[StepState]] = {
    StepState.PENDING: {StepState.RUNNING, StepState.FAILED},
    StepState.RUNNING: {StepState.SUCCEEDED, StepState.FAILED},
    StepState.SUCCEEDED: set(),
    StepState.FAILED: set(),
}

_SCHEMA_SQL = """
CREATE TABLE argus_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE missions (
    mission_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE steps (
    mission_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    operation TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_version INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    result_schema_version INTEGER,
    result_kind TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (mission_id, step_id),
    UNIQUE (mission_id, ordinal),
    UNIQUE (mission_id, idempotency_key),
    FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
);

CREATE TABLE journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id TEXT NOT NULL,
    step_id TEXT,
    entity_type TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
);

CREATE INDEX journal_mission_sequence_idx
    ON journal(mission_id, sequence);
"""


class SqliteMissionStore:
    """Transactional local durable state for Phase 1 missions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate_schema()
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise CorruptStateError(f"invalid SQLite mission store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SqliteMissionStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def create_mission(
        self,
        mission_id: str,
        steps: Sequence[StepEnvelope],
    ) -> MissionRecord:
        if not mission_id:
            raise ValueError("mission_id must be non-empty")
        self._validate_step_definitions(mission_id, steps)

        existing = self._find_mission(mission_id)
        if existing is not None:
            persisted = self.list_steps(mission_id)
            if [record.envelope for record in persisted] == list(steps):
                return existing
            raise DuplicateMissionError(
                f"mission {mission_id!r} already exists with a different definition"
            )

        now = _utc_now()
        try:
            with self._connection:
                self._connection.execute(
                    """
                    INSERT INTO missions(mission_id, schema_version, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (mission_id, CURRENT_SCHEMA_VERSION, MissionState.PENDING.value, now, now),
                )
                self._append_journal(
                    mission_id=mission_id,
                    step_id=None,
                    entity_type="mission",
                    event_type="created",
                    from_state=None,
                    to_state=MissionState.PENDING.value,
                    recorded_at=now,
                )
                for step in steps:
                    self._connection.execute(
                        """
                        INSERT INTO steps(
                            mission_id, step_id, ordinal, schema_version, operation,
                            payload_json, payload_version, idempotency_key, state,
                            result_schema_version, result_kind, result_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            step.mission_id,
                            step.step_id,
                            step.ordinal,
                            step.schema_version,
                            step.operation,
                            canonical_json(step.payload),
                            step.payload_version,
                            step.idempotency_key,
                            StepState.PENDING.value,
                            now,
                            now,
                        ),
                    )
                    self._append_journal(
                        mission_id=mission_id,
                        step_id=step.step_id,
                        entity_type="step",
                        event_type="created",
                        from_state=None,
                        to_state=StepState.PENDING.value,
                        recorded_at=now,
                    )
        except sqlite3.IntegrityError as exc:
            raise DuplicateMissionError(
                f"mission {mission_id!r} conflicts with durable state"
            ) from exc

        return self.load_mission(mission_id)

    def load_mission(self, mission_id: str) -> MissionRecord:
        record = self._find_mission(mission_id)
        if record is None:
            raise KeyError(f"unknown mission: {mission_id}")
        return record

    def list_steps(self, mission_id: str) -> list[StepRecord]:
        if self._find_mission(mission_id) is None:
            raise KeyError(f"unknown mission: {mission_id}")
        rows = self._connection.execute(
            "SELECT * FROM steps WHERE mission_id = ? ORDER BY ordinal ASC",
            (mission_id,),
        ).fetchall()
        records = [self._step_from_row(row) for row in rows]
        ordinals = [record.envelope.ordinal for record in records]
        if ordinals != list(range(len(records))):
            raise CorruptStateError(
                f"mission {mission_id!r} has non-contiguous step ordinals: {ordinals}"
            )
        return records

    def load_step(self, mission_id: str, step_id: str) -> StepRecord:
        row = self._connection.execute(
            "SELECT * FROM steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown step: {mission_id}/{step_id}")
        return self._step_from_row(row)

    def transition_mission(self, mission_id: str, target: MissionState) -> MissionRecord:
        with self._connection:
            current = self.load_mission(mission_id)
            if current.state == target:
                return current
            if target not in _MISSION_TRANSITIONS[current.state]:
                raise InvalidTransitionError(
                    f"invalid mission transition: {current.state.value} -> {target.value}"
                )
            if target is MissionState.COMPLETED:
                remaining = self._connection.execute(
                    "SELECT COUNT(*) FROM steps WHERE mission_id = ? AND state != ?",
                    (mission_id, StepState.SUCCEEDED.value),
                ).fetchone()[0]
                if remaining:
                    raise InvalidTransitionError(
                        "mission cannot complete while steps are not succeeded"
                    )
            now = _utc_now()
            self._connection.execute(
                "UPDATE missions SET state = ?, updated_at = ? WHERE mission_id = ?",
                (target.value, now, mission_id),
            )
            self._append_journal(
                mission_id=mission_id,
                step_id=None,
                entity_type="mission",
                event_type="state_transition",
                from_state=current.state.value,
                to_state=target.value,
                recorded_at=now,
            )
        return self.load_mission(mission_id)

    def transition_step(
        self,
        mission_id: str,
        step_id: str,
        target: StepState,
        *,
        result: StepResult | None = None,
    ) -> StepRecord:
        _validate_result_for_state(target, result)
        with self._connection:
            current = self.load_step(mission_id, step_id)
            if current.state == target:
                if current.result != result:
                    raise InvalidTransitionError(
                        "idempotent step transition supplied a different result"
                    )
                return current
            if target not in _STEP_TRANSITIONS[current.state]:
                raise InvalidTransitionError(
                    f"invalid step transition: {current.state.value} -> {target.value}"
                )
            now = _utc_now()
            if result is None:
                result_schema_version = None
                result_kind = None
                result_json = None
            else:
                result_schema_version = result.schema_version
                result_kind = result.kind.value
                result_json = canonical_json(result.output)
            self._connection.execute(
                """
                UPDATE steps
                   SET state = ?, result_schema_version = ?, result_kind = ?,
                       result_json = ?, updated_at = ?
                 WHERE mission_id = ? AND step_id = ?
                """,
                (
                    target.value,
                    result_schema_version,
                    result_kind,
                    result_json,
                    now,
                    mission_id,
                    step_id,
                ),
            )
            self._append_journal(
                mission_id=mission_id,
                step_id=step_id,
                entity_type="step",
                event_type="state_transition",
                from_state=current.state.value,
                to_state=target.value,
                recorded_at=now,
            )
        return self.load_step(mission_id, step_id)

    def journal(self, mission_id: str) -> list[JournalEntry]:
        if self._find_mission(mission_id) is None:
            raise KeyError(f"unknown mission: {mission_id}")
        rows = self._connection.execute(
            "SELECT * FROM journal WHERE mission_id = ? ORDER BY sequence ASC",
            (mission_id,),
        ).fetchall()
        return [
            JournalEntry(
                sequence=row["sequence"],
                mission_id=row["mission_id"],
                step_id=row["step_id"],
                entity_type=row["entity_type"],
                event_type=row["event_type"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def _initialize_or_validate_schema(self) -> None:
        quick_check = self._connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise CorruptStateError(f"SQLite quick_check failed: {quick_check}")

        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if not tables:
            with self._connection:
                self._connection.executescript(_SCHEMA_SQL)
                self._connection.execute(
                    "INSERT INTO argus_metadata(key, value) VALUES (?, ?)",
                    ("storage_schema_version", str(_STORAGE_SCHEMA_VERSION)),
                )
            return

        if not _REQUIRED_TABLES.issubset(tables):
            missing = sorted(_REQUIRED_TABLES - tables)
            raise CorruptStateError(f"mission store is missing required tables: {missing}")
        row = self._connection.execute(
            "SELECT value FROM argus_metadata WHERE key = ?",
            ("storage_schema_version",),
        ).fetchone()
        if row is None:
            raise CorruptStateError("mission store has no storage schema version")
        try:
            version = int(row[0])
        except (TypeError, ValueError) as exc:
            raise CorruptStateError("mission store schema version is invalid") from exc
        if version != _STORAGE_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported storage schema version: {version}"
            )

    def _find_mission(self, mission_id: str) -> MissionRecord | None:
        row = self._connection.execute(
            "SELECT * FROM missions WHERE mission_id = ?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        schema_version = row["schema_version"]
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported mission schema version: {schema_version}"
            )
        try:
            state = MissionState(row["state"])
        except ValueError as exc:
            raise CorruptStateError(
                f"mission {mission_id!r} has invalid state: {row['state']!r}"
            ) from exc
        return MissionRecord(
            mission_id=row["mission_id"],
            state=state,
            schema_version=schema_version,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _step_from_row(self, row: sqlite3.Row) -> StepRecord:
        schema_version = row["schema_version"]
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported step schema version: {schema_version}"
            )
        payload = _parse_mapping_json(row["payload_json"], "step payload")
        envelope = StepEnvelope(
            mission_id=row["mission_id"],
            step_id=row["step_id"],
            ordinal=row["ordinal"],
            operation=row["operation"],
            payload=payload,
            payload_version=row["payload_version"],
            schema_version=schema_version,
            idempotency_key=row["idempotency_key"],
        )
        if envelope.idempotency_key != derive_idempotency_key(envelope):
            raise CorruptStateError(
                f"step {envelope.mission_id}/{envelope.step_id} has invalid idempotency key"
            )
        try:
            state = StepState(row["state"])
        except ValueError as exc:
            raise CorruptStateError(
                f"step {envelope.mission_id}/{envelope.step_id} has invalid state"
            ) from exc

        result = self._result_from_row(row)
        if state in {StepState.SUCCEEDED, StepState.FAILED} and result is None:
            raise CorruptStateError("terminal step has no durable result")
        if state in {StepState.PENDING, StepState.RUNNING} and result is not None:
            raise CorruptStateError("non-terminal step unexpectedly has a result")
        if state is StepState.SUCCEEDED and result and result.kind is not StepResultKind.SUCCESS:
            raise CorruptStateError("succeeded step has a non-success result")
        if state is StepState.FAILED and result and result.kind is not StepResultKind.FAILURE:
            raise CorruptStateError("failed step has a non-failure result")

        return StepRecord(
            envelope=envelope,
            state=state,
            result=result,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _result_from_row(self, row: sqlite3.Row) -> StepResult | None:
        fields = (row["result_schema_version"], row["result_kind"], row["result_json"])
        if fields == (None, None, None):
            return None
        if any(value is None for value in fields):
            raise CorruptStateError("step result columns are partially populated")
        schema_version = int(row["result_schema_version"])
        if schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported step result schema version: {schema_version}"
            )
        try:
            kind = StepResultKind(row["result_kind"])
        except ValueError as exc:
            raise CorruptStateError("step result kind is invalid") from exc
        output = _parse_mapping_json(row["result_json"], "step result")
        return StepResult(kind=kind, output=output, schema_version=schema_version)

    def _validate_step_definitions(
        self,
        mission_id: str,
        steps: Sequence[StepEnvelope],
    ) -> None:
        if any(step.mission_id != mission_id for step in steps):
            raise ValueError("all steps must belong to the mission being created")
        ordinals = [step.ordinal for step in steps]
        if ordinals != list(range(len(steps))):
            raise ValueError("step ordinals must be contiguous and ordered from zero")
        step_ids = [step.step_id for step in steps]
        if len(set(step_ids)) != len(step_ids):
            raise ValueError("step ids must be unique within a mission")
        keys = [step.idempotency_key for step in steps]
        if len(set(keys)) != len(keys):
            raise ValueError("step idempotency keys must be unique within a mission")

    def _append_journal(
        self,
        *,
        mission_id: str,
        step_id: str | None,
        entity_type: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO journal(
                mission_id, step_id, entity_type, event_type,
                from_state, to_state, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission_id,
                step_id,
                entity_type,
                event_type,
                from_state,
                to_state,
                recorded_at,
            ),
        )


def _validate_result_for_state(target: StepState, result: StepResult | None) -> None:
    if target is StepState.SUCCEEDED:
        if result is None or result.kind is not StepResultKind.SUCCESS:
            raise InvalidTransitionError("succeeded step requires a success result")
    elif target is StepState.FAILED:
        if result is None or result.kind is not StepResultKind.FAILURE:
            raise InvalidTransitionError("failed step requires a failure result")
    elif result is not None:
        raise InvalidTransitionError("non-terminal step transitions cannot persist a result")


def _parse_mapping_json(raw: str, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CorruptStateError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise CorruptStateError(f"{label} must be a JSON object")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
