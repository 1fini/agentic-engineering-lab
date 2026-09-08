"""Durable Phase 3 external-effect intent and receipt state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from argus.model import ArgusStateError, UnsupportedSchemaVersionError, canonical_json
from argus.scheduler import canonical_due_at

_EFFECT_SCHEMA_VERSION = 1
_EFFECT_CONTRACT_VERSION = 1


class EffectError(ArgusStateError):
    """Base error for durable external-effect state."""


class DuplicateEffectError(EffectError):
    """Raised when an effect identity conflicts with durable state."""


class AmbiguousEffectError(EffectError):
    """Raised when an external effect may already have happened."""


class EffectState(StrEnum):
    INTENT_COMMITTED = "intent_committed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"


class EffectReceiptOutcome(StrEnum):
    APPLIED = "applied"
    NOT_APPLIED = "not_applied"


class EffectReceiptSource(StrEnum):
    EXECUTION = "execution"
    RECONCILIATION = "reconciliation"


@dataclass(frozen=True)
class EffectIntent:
    mission_id: str
    step_id: str
    effect_id: str
    operation: str
    payload: Mapping[str, Any]
    payload_version: int = 1
    schema_version: int = _EFFECT_CONTRACT_VERSION
    correlation_key: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != _EFFECT_CONTRACT_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported effect intent schema version: {self.schema_version}"
            )
        if not self.mission_id or not self.step_id or not self.effect_id or not self.operation:
            raise ValueError(
                "mission_id, step_id, effect_id, and operation must be non-empty"
            )
        if self.payload_version < 1:
            raise ValueError("payload_version must be >= 1")
        canonical_json(self.payload)
        derived = derive_effect_correlation_key(self)
        if self.correlation_key is None:
            object.__setattr__(self, "correlation_key", derived)
        elif self.correlation_key != derived:
            raise ValueError("effect correlation_key does not match durable intent")


@dataclass(frozen=True)
class EffectReceipt:
    correlation_key: str
    outcome: EffectReceiptOutcome
    evidence: Mapping[str, Any]
    source: EffectReceiptSource = EffectReceiptSource.EXECUTION
    schema_version: int = _EFFECT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _EFFECT_CONTRACT_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported effect receipt schema version: {self.schema_version}"
            )
        if not self.correlation_key:
            raise ValueError("receipt correlation_key must be non-empty")
        canonical_json(self.evidence)


@dataclass(frozen=True)
class EffectRecord:
    intent: EffectIntent
    state: EffectState
    receipt: EffectReceipt | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class EffectEvent:
    sequence: int
    mission_id: str
    step_id: str
    effect_id: str
    event_type: str
    from_state: str | None
    to_state: str | None
    recorded_at: str


def derive_effect_correlation_key(intent: EffectIntent) -> str:
    material = {
        "schema_version": intent.schema_version,
        "mission_id": intent.mission_id,
        "step_id": intent.step_id,
        "effect_id": intent.effect_id,
        "operation": intent.operation,
        "payload_version": intent.payload_version,
        "payload": intent.payload,
    }
    digest = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"argus:effect:v1:{digest}"


class EffectStore:
    """Persist Phase 3 effect intent, ambiguity, receipt, and audit history."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise EffectError("EffectStore requires a file-backed ARGUS store")
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
        except EffectError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise EffectError(f"invalid ARGUS effect store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EffectStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def create_intent(
        self,
        intent: EffectIntent,
        *,
        recorded_at: str | datetime | None = None,
    ) -> EffectRecord:
        timestamp = _canonical_time(recorded_at)
        with self._connection:
            step = self._connection.execute(
                "SELECT 1 FROM steps WHERE mission_id = ? AND step_id = ?",
                (intent.mission_id, intent.step_id),
            ).fetchone()
            if step is None:
                raise KeyError(f"unknown step: {intent.mission_id}/{intent.step_id}")

            existing_row = self._connection.execute(
                """
                SELECT * FROM effects
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (intent.mission_id, intent.step_id, intent.effect_id),
            ).fetchone()
            if existing_row is not None:
                existing = self._record_from_row(existing_row)
                if existing.intent == intent:
                    return existing
                raise DuplicateEffectError(
                    f"effect {intent.mission_id}/{intent.step_id}/{intent.effect_id} "
                    "already exists with a different durable intent"
                )

            try:
                self._connection.execute(
                    """
                    INSERT INTO effects(
                        mission_id, step_id, effect_id, schema_version, operation,
                        payload_json, payload_version, correlation_key, state,
                        receipt_schema_version, receipt_outcome, receipt_source,
                        receipt_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    (
                        intent.mission_id,
                        intent.step_id,
                        intent.effect_id,
                        intent.schema_version,
                        intent.operation,
                        canonical_json(intent.payload),
                        intent.payload_version,
                        intent.correlation_key,
                        EffectState.INTENT_COMMITTED.value,
                        timestamp,
                        timestamp,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateEffectError(
                    f"effect correlation conflicts with durable state: {intent.correlation_key}"
                ) from exc
            self._append_event(
                intent.mission_id,
                intent.step_id,
                intent.effect_id,
                event_type="intent_committed",
                from_state=None,
                to_state=EffectState.INTENT_COMMITTED.value,
                recorded_at=timestamp,
            )
        return self.load(intent.mission_id, intent.step_id, intent.effect_id)

    def begin_execution(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        *,
        recorded_at: str | datetime | None = None,
    ) -> EffectRecord:
        """Cross the durable ambiguity boundary immediately before an external call."""
        timestamp = _canonical_time(recorded_at)
        with self._connection:
            current = self._load_for_update(mission_id, step_id, effect_id)
            if current.state is EffectState.OUTCOME_UNKNOWN:
                raise AmbiguousEffectError(
                    f"effect {current.intent.correlation_key} already has unknown external outcome"
                )
            if current.state is not EffectState.INTENT_COMMITTED:
                raise EffectError(
                    f"effect cannot begin execution from {current.state.value}"
                )
            self._connection.execute(
                """
                UPDATE effects SET state = ?, updated_at = ?
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (
                    EffectState.OUTCOME_UNKNOWN.value,
                    timestamp,
                    mission_id,
                    step_id,
                    effect_id,
                ),
            )
            self._append_event(
                mission_id,
                step_id,
                effect_id,
                event_type="execution_boundary_entered",
                from_state=EffectState.INTENT_COMMITTED.value,
                to_state=EffectState.OUTCOME_UNKNOWN.value,
                recorded_at=timestamp,
            )
        return self.load(mission_id, step_id, effect_id)

    def record_receipt(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        receipt: EffectReceipt,
        *,
        recorded_at: str | datetime | None = None,
    ) -> EffectRecord:
        timestamp = _canonical_time(recorded_at)
        with self._connection:
            current = self._load_for_update(mission_id, step_id, effect_id)
            if receipt.correlation_key != current.intent.correlation_key:
                raise EffectError("receipt correlation_key does not match durable effect intent")

            target = (
                EffectState.CONFIRMED_APPLIED
                if receipt.outcome is EffectReceiptOutcome.APPLIED
                else EffectState.CONFIRMED_NOT_APPLIED
            )

            if current.state in {
                EffectState.CONFIRMED_APPLIED,
                EffectState.CONFIRMED_NOT_APPLIED,
            }:
                if current.state is target and current.receipt == receipt:
                    return current
                raise EffectError(
                    f"effect already has a different terminal receipt: {current.state.value}"
                )
            if current.state is not EffectState.OUTCOME_UNKNOWN:
                raise EffectError(
                    f"effect receipt requires outcome_unknown state, got {current.state.value}"
                )

            self._connection.execute(
                """
                UPDATE effects
                   SET state = ?, receipt_schema_version = ?, receipt_outcome = ?,
                       receipt_source = ?, receipt_json = ?, updated_at = ?
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (
                    target.value,
                    receipt.schema_version,
                    receipt.outcome.value,
                    receipt.source.value,
                    canonical_json(receipt.evidence),
                    timestamp,
                    mission_id,
                    step_id,
                    effect_id,
                ),
            )
            self._append_event(
                mission_id,
                step_id,
                effect_id,
                event_type="receipt_committed",
                from_state=EffectState.OUTCOME_UNKNOWN.value,
                to_state=target.value,
                recorded_at=timestamp,
            )
        return self.load(mission_id, step_id, effect_id)

    def load(self, mission_id: str, step_id: str, effect_id: str) -> EffectRecord:
        row = self._connection.execute(
            """
            SELECT * FROM effects
             WHERE mission_id = ? AND step_id = ? AND effect_id = ?
            """,
            (mission_id, step_id, effect_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown effect: {mission_id}/{step_id}/{effect_id}")
        return self._record_from_row(row)

    def list_for_mission(self, mission_id: str) -> list[EffectRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM effects
             WHERE mission_id = ?
             ORDER BY step_id ASC, effect_id ASC
            """,
            (mission_id,),
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def history(
        self,
        mission_id: str,
        step_id: str | None = None,
        effect_id: str | None = None,
    ) -> list[EffectEvent]:
        query = "SELECT * FROM effect_events WHERE mission_id = ?"
        params: list[object] = [mission_id]
        if step_id is not None:
            query += " AND step_id = ?"
            params.append(step_id)
        if effect_id is not None:
            query += " AND effect_id = ?"
            params.append(effect_id)
        query += " ORDER BY sequence ASC"
        rows = self._connection.execute(query, params).fetchall()
        return [
            EffectEvent(
                sequence=row["sequence"],
                mission_id=row["mission_id"],
                step_id=row["step_id"],
                effect_id=row["effect_id"],
                event_type=row["event_type"],
                from_state=row["from_state"],
                to_state=row["to_state"],
                recorded_at=_validated_timestamp(row["recorded_at"]),
            )
            for row in rows
        ]

    def _load_for_update(self, mission_id: str, step_id: str, effect_id: str) -> EffectRecord:
        row = self._connection.execute(
            """
            SELECT * FROM effects
             WHERE mission_id = ? AND step_id = ? AND effect_id = ?
            """,
            (mission_id, step_id, effect_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown effect: {mission_id}/{step_id}/{effect_id}")
        return self._record_from_row(row)

    def _record_from_row(self, row: sqlite3.Row) -> EffectRecord:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise EffectError("effect intent payload is corrupt") from exc
        if not isinstance(payload, dict):
            raise EffectError("effect intent payload must be a JSON object")

        try:
            intent = EffectIntent(
                mission_id=row["mission_id"],
                step_id=row["step_id"],
                effect_id=row["effect_id"],
                operation=row["operation"],
                payload=payload,
                payload_version=row["payload_version"],
                schema_version=row["schema_version"],
                correlation_key=row["correlation_key"],
            )
            state = EffectState(row["state"])
        except (TypeError, ValueError, UnsupportedSchemaVersionError) as exc:
            raise EffectError("effect durable state is invalid") from exc

        receipt_fields = (
            row["receipt_schema_version"],
            row["receipt_outcome"],
            row["receipt_source"],
            row["receipt_json"],
        )
        receipt: EffectReceipt | None = None
        if state in {
            EffectState.CONFIRMED_APPLIED,
            EffectState.CONFIRMED_NOT_APPLIED,
        }:
            if any(value is None for value in receipt_fields):
                raise EffectError("confirmed effect is missing receipt fields")
            try:
                evidence = json.loads(row["receipt_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise EffectError("effect receipt evidence is corrupt") from exc
            if not isinstance(evidence, dict):
                raise EffectError("effect receipt evidence must be a JSON object")
            try:
                receipt = EffectReceipt(
                    correlation_key=intent.correlation_key or "",
                    outcome=EffectReceiptOutcome(row["receipt_outcome"]),
                    source=EffectReceiptSource(row["receipt_source"]),
                    evidence=evidence,
                    schema_version=row["receipt_schema_version"],
                )
            except (TypeError, ValueError, UnsupportedSchemaVersionError) as exc:
                raise EffectError("effect receipt is invalid") from exc
            expected_state = (
                EffectState.CONFIRMED_APPLIED
                if receipt.outcome is EffectReceiptOutcome.APPLIED
                else EffectState.CONFIRMED_NOT_APPLIED
            )
            if state is not expected_state:
                raise EffectError("effect state and receipt outcome disagree")
        elif any(value is not None for value in receipt_fields):
            raise EffectError("non-terminal effect unexpectedly contains receipt fields")

        return EffectRecord(
            intent=intent,
            state=state,
            receipt=receipt,
            created_at=_validated_timestamp(row["created_at"]),
            updated_at=_validated_timestamp(row["updated_at"]),
        )

    def _append_event(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
        *,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO effect_events(
                mission_id, step_id, effect_id, event_type,
                from_state, to_state, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission_id,
                step_id,
                effect_id,
                event_type,
                from_state,
                to_state,
                recorded_at,
            ),
        )

    def _initialize_or_validate(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"missions", "steps", "journal"}
        if not required.issubset(tables):
            raise EffectError("EffectStore requires initialized ARGUS mission state")

        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_effect_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS effects(
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_version INTEGER NOT NULL,
                    correlation_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    receipt_schema_version INTEGER,
                    receipt_outcome TEXT,
                    receipt_source TEXT,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(mission_id, step_id, effect_id),
                    FOREIGN KEY(mission_id, step_id)
                        REFERENCES steps(mission_id, step_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS effect_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(mission_id, step_id, effect_id)
                        REFERENCES effects(mission_id, step_id, effect_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS effect_events_mission_sequence_idx
                    ON effect_events(mission_id, sequence)
                """
            )
            row = self._connection.execute(
                "SELECT value FROM argus_effect_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO argus_effect_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_EFFECT_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise EffectError("effect schema version is invalid") from exc
                if version != _EFFECT_SCHEMA_VERSION:
                    raise EffectError(f"unsupported effect schema version: {version}")


def _canonical_time(value: str | datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    return canonical_due_at(value)


def _validated_timestamp(value: str) -> str:
    try:
        canonical = canonical_due_at(value)
    except (TypeError, ValueError) as exc:
        raise EffectError("effect timestamp is invalid") from exc
    if canonical != value:
        raise EffectError("effect timestamp is not canonical")
    return value
