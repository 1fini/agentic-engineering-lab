"""Generic durable state contracts for ARGUS Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping

CURRENT_SCHEMA_VERSION = 1


class MissionState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StepResultKind(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


class ArgusStateError(RuntimeError):
    """Base class for durable-state errors."""


class InvalidTransitionError(ArgusStateError):
    """Raised when a state transition violates the Phase 1 state machine."""


class UnsupportedSchemaVersionError(ArgusStateError):
    """Raised when persisted state uses an unsupported schema version."""


class CorruptStateError(ArgusStateError):
    """Raised when persisted state is malformed or internally inconsistent."""


class DuplicateMissionError(ArgusStateError):
    """Raised when a mission id is reused with a different durable definition."""


@dataclass(frozen=True)
class StepResult:
    kind: StepResultKind
    output: Mapping[str, Any]
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported step result schema version: {self.schema_version}"
            )
        _canonical_json(self.output)


@dataclass(frozen=True)
class StepEnvelope:
    mission_id: str
    step_id: str
    ordinal: int
    operation: str
    payload: Mapping[str, Any]
    payload_version: int = 1
    schema_version: int = CURRENT_SCHEMA_VERSION
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"unsupported step envelope schema version: {self.schema_version}"
            )
        if not self.mission_id or not self.step_id or not self.operation:
            raise ValueError("mission_id, step_id, and operation must be non-empty")
        if self.ordinal < 0:
            raise ValueError("ordinal must be non-negative")
        if self.payload_version < 1:
            raise ValueError("payload_version must be >= 1")
        _canonical_json(self.payload)
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", derive_idempotency_key(self))


@dataclass(frozen=True)
class MissionRecord:
    mission_id: str
    state: MissionState
    schema_version: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class StepRecord:
    envelope: StepEnvelope
    state: StepState
    result: StepResult | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class JournalEntry:
    sequence: int
    mission_id: str
    step_id: str | None
    entity_type: str
    event_type: str
    from_state: str | None
    to_state: str | None
    recorded_at: str


def derive_idempotency_key(envelope: StepEnvelope) -> str:
    material = {
        "schema_version": envelope.schema_version,
        "mission_id": envelope.mission_id,
        "step_id": envelope.step_id,
        "ordinal": envelope.ordinal,
        "operation": envelope.operation,
        "payload_version": envelope.payload_version,
        "payload": envelope.payload,
    }
    digest = hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()
    return f"argus:v1:{digest}"


def canonical_json(value: Mapping[str, Any]) -> str:
    """Return stable JSON for durable comparisons and hashing."""
    return _canonical_json(value)


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload/result must be JSON-serializable") from exc
