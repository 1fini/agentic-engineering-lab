"""Durable Phase 4 operator controls and execution-gate decisions for ARGUS."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import sqlite3

from argus.model import ArgusStateError

_GUARDRAIL_SCHEMA_VERSION = 1
_GLOBAL_SCOPE_KEY = "__global__"


class GuardrailError(ArgusStateError):
    """Raised when durable guardrail state is invalid or cannot be changed safely."""


class MissionControlState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class GateDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class GateReason(StrEnum):
    ALLOWED = "allowed"
    MISSION_CANCELLED = "mission_cancelled"
    GLOBAL_KILL = "global_kill"
    WORKLOAD_KILL = "workload_kill"
    MISSION_PAUSED = "mission_paused"


@dataclass(frozen=True)
class GuardrailPolicy:
    mission_id: str
    workload_scope: str
    policy_version: int = 1
    policy_ref: str | None = None
    schema_version: int = _GUARDRAIL_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.mission_id or not self.workload_scope:
            raise ValueError("mission_id and workload_scope must be non-empty")
        if self.policy_version < 1:
            raise ValueError("policy_version must be >= 1")
        if self.schema_version != _GUARDRAIL_SCHEMA_VERSION:
            raise GuardrailError(
                f"unsupported guardrail policy schema version: {self.schema_version}"
            )
        if self.policy_ref is not None and not self.policy_ref:
            raise ValueError("policy_ref must be non-empty when provided")

    @property
    def policy_hash(self) -> str:
        material = {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "workload_scope": self.workload_scope,
            "policy_version": self.policy_version,
            "policy_ref": self.policy_ref,
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return f"argus:guardrail:v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class MissionGuardrailRecord:
    policy: GuardrailPolicy
    control_state: MissionControlState
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class GateResult:
    mission_id: str
    workload_scope: str
    decision: GateDecision
    reason: GateReason
    policy_version: int
    policy_hash: str


@dataclass(frozen=True)
class GuardrailEvent:
    sequence: int
    mission_id: str | None
    workload_scope: str | None
    event_type: str
    decision: str | None
    reason: str | None
    policy_version: int | None
    policy_hash: str | None
    recorded_at: str


class GuardrailStore:
    """Persist mission controls, kill switches, and audited gate decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise GuardrailError(
                "GuardrailStore requires a file-backed ARGUS store; :memory: cannot be shared"
            )
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
        except GuardrailError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise GuardrailError(f"invalid ARGUS guardrail store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "GuardrailStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def set_policy(self, policy: GuardrailPolicy) -> MissionGuardrailRecord:
        self._require_mission(policy.mission_id)
        existing = self.get(policy.mission_id)
        if existing is not None:
            if existing.policy == policy:
                return existing
            if existing.policy.workload_scope != policy.workload_scope:
                raise GuardrailError("workload_scope is immutable for an initialized mission")
            if policy.policy_version <= existing.policy.policy_version:
                raise GuardrailError(
                    "guardrail policy updates require a strictly increasing policy_version"
                )
            state = existing.control_state
            created_at = existing.created_at
            event_type = "policy_updated"
        else:
            state = MissionControlState.ACTIVE
            created_at = _utc_now()
            event_type = "policy_created"

        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO mission_guardrails(
                    mission_id, workload_scope, policy_version, policy_ref, policy_hash,
                    control_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    policy_ref = excluded.policy_ref,
                    policy_hash = excluded.policy_hash,
                    control_state = excluded.control_state,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.mission_id,
                    policy.workload_scope,
                    policy.policy_version,
                    policy.policy_ref,
                    policy.policy_hash,
                    state.value,
                    created_at,
                    now,
                ),
            )
            self._append_event(
                mission_id=policy.mission_id,
                workload_scope=policy.workload_scope,
                event_type=event_type,
                decision=None,
                reason=None,
                policy_version=policy.policy_version,
                policy_hash=policy.policy_hash,
                recorded_at=now,
            )
        return self.load(policy.mission_id)

    def get(self, mission_id: str) -> MissionGuardrailRecord | None:
        row = self._connection.execute(
            "SELECT * FROM mission_guardrails WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def load(self, mission_id: str) -> MissionGuardrailRecord:
        record = self.get(mission_id)
        if record is None:
            raise KeyError(f"mission guardrail policy is not initialized: {mission_id}")
        return record

    def pause(self, mission_id: str) -> MissionGuardrailRecord:
        return self._transition_control(mission_id, MissionControlState.PAUSED, "paused")

    def resume(self, mission_id: str) -> MissionGuardrailRecord:
        current = self.load(mission_id)
        if current.control_state is MissionControlState.CANCELLED:
            raise GuardrailError("cancelled mission cannot be resumed")
        return self._transition_control(mission_id, MissionControlState.ACTIVE, "resumed")

    def cancel(self, mission_id: str) -> MissionGuardrailRecord:
        return self._transition_control(mission_id, MissionControlState.CANCELLED, "cancelled")

    def set_global_kill(self, enabled: bool) -> bool:
        return self._set_kill("global", _GLOBAL_SCOPE_KEY, enabled)

    def set_workload_kill(self, workload_scope: str, enabled: bool) -> bool:
        if not workload_scope:
            raise ValueError("workload_scope must be non-empty")
        return self._set_kill("workload", workload_scope, enabled)

    def global_kill_enabled(self) -> bool:
        return self._kill_enabled("global", _GLOBAL_SCOPE_KEY)

    def workload_kill_enabled(self, workload_scope: str) -> bool:
        if not workload_scope:
            raise ValueError("workload_scope must be non-empty")
        return self._kill_enabled("workload", workload_scope)

    def check_gate(self, mission_id: str) -> GateResult:
        """Return the current gate decision without mutating durable state."""
        record = self.load(mission_id)
        if record.control_state is MissionControlState.CANCELLED:
            reason = GateReason.MISSION_CANCELLED
        elif self.global_kill_enabled():
            reason = GateReason.GLOBAL_KILL
        elif self.workload_kill_enabled(record.policy.workload_scope):
            reason = GateReason.WORKLOAD_KILL
        elif record.control_state is MissionControlState.PAUSED:
            reason = GateReason.MISSION_PAUSED
        else:
            reason = GateReason.ALLOWED
        return GateResult(
            mission_id=mission_id,
            workload_scope=record.policy.workload_scope,
            decision=(GateDecision.ALLOW if reason is GateReason.ALLOWED else GateDecision.DENY),
            reason=reason,
            policy_version=record.policy.policy_version,
            policy_hash=record.policy.policy_hash,
        )

    def audit_gate(self, mission_id: str) -> GateResult:
        """Evaluate and append an explicit gate decision audit record."""
        result = self.check_gate(mission_id)
        with self._connection:
            self._append_event(
                mission_id=result.mission_id,
                workload_scope=result.workload_scope,
                event_type="gate_decision",
                decision=result.decision.value,
                reason=result.reason.value,
                policy_version=result.policy_version,
                policy_hash=result.policy_hash,
                recorded_at=_utc_now(),
            )
        return result

    def history(self, mission_id: str | None = None) -> list[GuardrailEvent]:
        if mission_id is None:
            rows = self._connection.execute(
                "SELECT * FROM guardrail_events ORDER BY sequence ASC"
            ).fetchall()
        else:
            rows = self._connection.execute(
                "SELECT * FROM guardrail_events WHERE mission_id = ? ORDER BY sequence ASC",
                (mission_id,),
            ).fetchall()
        return [
            GuardrailEvent(
                sequence=row["sequence"],
                mission_id=row["mission_id"],
                workload_scope=row["workload_scope"],
                event_type=row["event_type"],
                decision=row["decision"],
                reason=row["reason"],
                policy_version=row["policy_version"],
                policy_hash=row["policy_hash"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def _transition_control(
        self,
        mission_id: str,
        target: MissionControlState,
        event_type: str,
    ) -> MissionGuardrailRecord:
        current = self.load(mission_id)
        if current.control_state is target:
            return current
        if current.control_state is MissionControlState.CANCELLED:
            raise GuardrailError("cancelled mission control state is terminal")
        if target is MissionControlState.ACTIVE and current.control_state is not MissionControlState.PAUSED:
            raise GuardrailError("mission may resume only from paused state")
        if target is MissionControlState.PAUSED and current.control_state is not MissionControlState.ACTIVE:
            raise GuardrailError("mission may pause only from active state")
        if target is MissionControlState.CANCELLED and current.control_state not in {
            MissionControlState.ACTIVE,
            MissionControlState.PAUSED,
        }:
            raise GuardrailError("mission cannot be cancelled from current state")

        now = _utc_now()
        with self._connection:
            self._connection.execute(
                "UPDATE mission_guardrails SET control_state = ?, updated_at = ? WHERE mission_id = ?",
                (target.value, now, mission_id),
            )
            self._append_event(
                mission_id=mission_id,
                workload_scope=current.policy.workload_scope,
                event_type=event_type,
                decision=None,
                reason=None,
                policy_version=current.policy.policy_version,
                policy_hash=current.policy.policy_hash,
                recorded_at=now,
            )
        return self.load(mission_id)

    def _set_kill(self, scope_type: str, scope_key: str, enabled: bool) -> bool:
        existing = self._connection.execute(
            "SELECT enabled FROM kill_switches WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ).fetchone()
        current = bool(existing[0]) if existing is not None else False
        if current == bool(enabled):
            return current
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO kill_switches(scope_type, scope_key, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_key) DO UPDATE SET
                    enabled = excluded.enabled,
                    updated_at = excluded.updated_at
                """,
                (scope_type, scope_key, int(bool(enabled)), now),
            )
            self._append_event(
                mission_id=None,
                workload_scope=None if scope_type == "global" else scope_key,
                event_type=(
                    f"{scope_type}_kill_enabled" if enabled else f"{scope_type}_kill_disabled"
                ),
                decision=None,
                reason=None,
                policy_version=None,
                policy_hash=None,
                recorded_at=now,
            )
        return bool(enabled)

    def _kill_enabled(self, scope_type: str, scope_key: str) -> bool:
        row = self._connection.execute(
            "SELECT enabled FROM kill_switches WHERE scope_type = ? AND scope_key = ?",
            (scope_type, scope_key),
        ).fetchone()
        if row is None:
            return False
        if row[0] not in (0, 1):
            raise GuardrailError("persisted kill switch has invalid enabled value")
        return bool(row[0])

    def _require_mission(self, mission_id: str) -> None:
        row = self._connection.execute(
            "SELECT 1 FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown mission: {mission_id}")

    def _initialize_or_validate(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"missions", "steps", "argus_metadata"}.issubset(tables):
            raise GuardrailError(
                "guardrails require an initialized ARGUS mission store"
            )
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_guardrail_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mission_guardrails (
                    mission_id TEXT PRIMARY KEY,
                    workload_scope TEXT NOT NULL,
                    policy_version INTEGER NOT NULL,
                    policy_ref TEXT,
                    policy_hash TEXT NOT NULL,
                    control_state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS kill_switches (
                    scope_type TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(scope_type, scope_key)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS guardrail_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT,
                    workload_scope TEXT,
                    event_type TEXT NOT NULL,
                    decision TEXT,
                    reason TEXT,
                    policy_version INTEGER,
                    policy_hash TEXT,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            existing = self._connection.execute(
                "SELECT value FROM argus_guardrail_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO argus_guardrail_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_GUARDRAIL_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(existing[0])
                except (TypeError, ValueError) as exc:
                    raise GuardrailError("guardrail schema version is invalid") from exc
                if version != _GUARDRAIL_SCHEMA_VERSION:
                    raise GuardrailError(
                        f"unsupported guardrail schema version: {version}"
                    )
        self._validate_persisted_rows()

    def _validate_persisted_rows(self) -> None:
        rows = self._connection.execute("SELECT * FROM mission_guardrails").fetchall()
        for row in rows:
            try:
                state = MissionControlState(row["control_state"])
                policy = GuardrailPolicy(
                    mission_id=row["mission_id"],
                    workload_scope=row["workload_scope"],
                    policy_version=int(row["policy_version"]),
                    policy_ref=row["policy_ref"],
                )
            except (ValueError, TypeError) as exc:
                raise GuardrailError("invalid persisted mission guardrail row") from exc
            if state not in MissionControlState:
                raise GuardrailError("invalid persisted control state")
            if policy.policy_hash != row["policy_hash"]:
                raise GuardrailError(
                    f"persisted guardrail policy hash mismatch for {row['mission_id']}"
                )
        kills = self._connection.execute("SELECT * FROM kill_switches").fetchall()
        for row in kills:
            if row["scope_type"] not in {"global", "workload"}:
                raise GuardrailError("invalid persisted kill-switch scope type")
            if row["enabled"] not in (0, 1):
                raise GuardrailError("invalid persisted kill-switch enabled value")
            if row["scope_type"] == "global" and row["scope_key"] != _GLOBAL_SCOPE_KEY:
                raise GuardrailError("invalid persisted global kill-switch key")

    def _record_from_row(self, row: sqlite3.Row) -> MissionGuardrailRecord:
        try:
            policy = GuardrailPolicy(
                mission_id=row["mission_id"],
                workload_scope=row["workload_scope"],
                policy_version=int(row["policy_version"]),
                policy_ref=row["policy_ref"],
            )
            state = MissionControlState(row["control_state"])
        except (ValueError, TypeError) as exc:
            raise GuardrailError("invalid persisted mission guardrail row") from exc
        if policy.policy_hash != row["policy_hash"]:
            raise GuardrailError(
                f"persisted guardrail policy hash mismatch for {row['mission_id']}"
            )
        return MissionGuardrailRecord(
            policy=policy,
            control_state=state,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _append_event(
        self,
        *,
        mission_id: str | None,
        workload_scope: str | None,
        event_type: str,
        decision: str | None,
        reason: str | None,
        policy_version: int | None,
        policy_hash: str | None,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO guardrail_events(
                mission_id, workload_scope, event_type, decision, reason,
                policy_version, policy_hash, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission_id,
                workload_scope,
                event_type,
                decision,
                reason,
                policy_version,
                policy_hash,
                recorded_at,
            ),
        )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
