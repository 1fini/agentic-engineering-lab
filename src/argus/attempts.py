"""Durable Phase 2 worker attempts and deterministic retry policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import math
from pathlib import Path
import sqlite3
from typing import Protocol

from argus.model import ArgusStateError, StepResultKind, canonical_json
from argus.scheduler import DurableScheduler, canonical_due_at
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerRequest

_ATTEMPT_SCHEMA_VERSION = 1


class AttemptError(ArgusStateError):
    """Base error for durable Phase 2 attempt state."""


class AmbiguousAttemptError(AttemptError):
    """Raised when replay could duplicate an external computation/effect."""


class AttemptNotDueError(AttemptError):
    """Raised when a caller tries to execute work before durable eligibility."""


class AttemptState(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"


class AttemptDecisionKind(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class AttemptPolicy:
    max_attempts: int = 3
    retry_delay_seconds: float = 60.0
    retry_process_errors: bool = True
    retry_timeouts: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.max_attempts, int) or isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if (
            isinstance(self.retry_delay_seconds, bool)
            or not isinstance(self.retry_delay_seconds, (int, float))
            or not math.isfinite(float(self.retry_delay_seconds))
            or self.retry_delay_seconds < 0
        ):
            raise ValueError("retry_delay_seconds must be a finite non-negative number")


@dataclass(frozen=True)
class AttemptRecord:
    mission_id: str
    step_id: str
    attempt_no: int
    request_id: str
    state: AttemptState
    started_at: str
    completed_at: str | None
    outcome: InvocationOutcome | None
    duration_ms: int | None
    exit_code: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    retry_due_at: str | None
    detail: str | None


@dataclass(frozen=True)
class AttemptDecision:
    kind: AttemptDecisionKind
    attempt: AttemptRecord
    retry_due_at: str | None = None


class WorkerInvoker(Protocol):
    def invoke(self, request: WorkerRequest) -> WorkerInvocation: ...


class AttemptStore:
    """Persist attempt metadata and atomically apply terminal/retry decisions."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise AttemptError("AttemptStore requires a file-backed ARGUS store")
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
        except AttemptError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise AttemptError(f"invalid ARGUS attempt store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "AttemptStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def list_attempts(self, mission_id: str, step_id: str) -> list[AttemptRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM attempts
             WHERE mission_id = ? AND step_id = ?
             ORDER BY attempt_no ASC
            """,
            (mission_id, step_id),
        ).fetchall()
        return [_attempt_from_row(row) for row in rows]

    def latest_attempt(self, mission_id: str, step_id: str) -> AttemptRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM attempts
             WHERE mission_id = ? AND step_id = ?
             ORDER BY attempt_no DESC LIMIT 1
            """,
            (mission_id, step_id),
        ).fetchone()
        return _attempt_from_row(row) if row is not None else None

    def begin_attempt(self, mission_id: str, step_id: str, *, started_at: str | datetime) -> AttemptRecord:
        started = canonical_due_at(started_at)
        with self._connection:
            step = self._connection.execute(
                "SELECT state FROM steps WHERE mission_id = ? AND step_id = ?",
                (mission_id, step_id),
            ).fetchone()
            if step is None:
                raise KeyError(f"unknown step: {mission_id}/{step_id}")
            if step["state"] != "pending":
                raise AttemptError(
                    f"attempts require a pending step: {mission_id}/{step_id} is {step['state']}"
                )

            latest_row = self._connection.execute(
                """
                SELECT * FROM attempts
                 WHERE mission_id = ? AND step_id = ?
                 ORDER BY attempt_no DESC LIMIT 1
                """,
                (mission_id, step_id),
            ).fetchone()
            latest = _attempt_from_row(latest_row) if latest_row is not None else None
            if latest is not None and latest.state is AttemptState.STARTED:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} was started without a durable outcome; replay is blocked"
                )
            if latest is not None and latest.outcome is InvocationOutcome.TERMINATION_UNCERTAIN:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has uncertain process-tree termination; replay is blocked"
                )

            attempt_no = 1 if latest is None else latest.attempt_no + 1
            request_id = f"{mission_id}/{step_id}/attempt-{attempt_no}"
            mission = self._connection.execute(
                "SELECT state FROM missions WHERE mission_id = ?",
                (mission_id,),
            ).fetchone()
            if mission is None:
                raise KeyError(f"unknown mission: {mission_id}")
            if mission["state"] in {"completed", "failed"}:
                raise AttemptError(f"mission {mission_id!r} is terminal")
            if mission["state"] == "pending":
                self._connection.execute(
                    "UPDATE missions SET state = 'running', updated_at = ? WHERE mission_id = ?",
                    (started, mission_id),
                )
                self._append_core_journal(
                    mission_id=mission_id,
                    step_id=None,
                    entity_type="mission",
                    from_state="pending",
                    to_state="running",
                    recorded_at=started,
                )

            self._connection.execute(
                """
                INSERT INTO attempts(
                    mission_id, step_id, attempt_no, request_id, state, started_at,
                    completed_at, outcome, duration_ms, exit_code, input_tokens,
                    output_tokens, cost_usd, retry_due_at, detail
                ) VALUES (?, ?, ?, ?, 'started', ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)
                """,
                (mission_id, step_id, attempt_no, request_id, started),
            )
            self._append_attempt_event(
                mission_id,
                step_id,
                attempt_no,
                "started",
                started,
            )
        result = self.latest_attempt(mission_id, step_id)
        assert result is not None
        return result

    def complete_attempt(
        self,
        attempt: AttemptRecord,
        invocation: WorkerInvocation,
        *,
        completed_at: str | datetime,
        policy: AttemptPolicy,
    ) -> AttemptDecision:
        completed = canonical_due_at(completed_at)
        usage = invocation.response.usage if invocation.response is not None else None
        detail = _bounded_detail(invocation.detail)

        retryable = _is_retryable(invocation, policy)
        exhausted = attempt.attempt_no >= policy.max_attempts
        retry_due: str | None = None
        if retryable and not exhausted:
            delay = policy.retry_delay_seconds
            if (
                invocation.response is not None
                and invocation.response.retry_after_seconds is not None
            ):
                delay = float(invocation.response.retry_after_seconds)
            completed_dt = _parse_canonical(completed)
            retry_due = canonical_due_at(completed_dt + timedelta(seconds=delay))

        with self._connection:
            row = self._connection.execute(
                "SELECT * FROM attempts WHERE request_id = ?",
                (attempt.request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown attempt: {attempt.request_id}")
            current = _attempt_from_row(row)
            if current.state is AttemptState.COMPLETED:
                raise AttemptError(f"attempt {attempt.request_id} is already completed")

            self._connection.execute(
                """
                UPDATE attempts
                   SET state = 'completed', completed_at = ?, outcome = ?, duration_ms = ?,
                       exit_code = ?, input_tokens = ?, output_tokens = ?, cost_usd = ?,
                       retry_due_at = ?, detail = ?
                 WHERE request_id = ?
                """,
                (
                    completed,
                    invocation.outcome.value,
                    invocation.duration_ms,
                    invocation.exit_code,
                    usage.input_tokens if usage else None,
                    usage.output_tokens if usage else None,
                    float(usage.cost_usd) if usage and usage.cost_usd is not None else None,
                    retry_due,
                    detail,
                    attempt.request_id,
                ),
            )
            self._append_attempt_event(
                attempt.mission_id,
                attempt.step_id,
                attempt.attempt_no,
                "completed",
                completed,
            )

            if invocation.outcome is InvocationOutcome.TERMINATION_UNCERTAIN:
                decision = AttemptDecisionKind.BLOCKED
            elif invocation.outcome is InvocationOutcome.SUCCESS:
                response = invocation.response
                if response is None:
                    raise AttemptError("successful invocation has no validated worker response")
                self._finalize_core_step(
                    attempt.mission_id,
                    attempt.step_id,
                    final_state="succeeded",
                    result_kind=StepResultKind.SUCCESS.value,
                    result_output=dict(response.output),
                    recorded_at=completed,
                )
                self._clear_schedule(attempt.mission_id, attempt.step_id, completed)
                decision = AttemptDecisionKind.SUCCEEDED
            elif retry_due is not None:
                self._upsert_schedule(attempt.mission_id, attempt.step_id, retry_due, completed)
                decision = AttemptDecisionKind.RETRY_SCHEDULED
            else:
                reason = invocation.outcome.value
                if retryable and exhausted:
                    reason = f"attempt_limit_exhausted:{reason}"
                self._finalize_core_step(
                    attempt.mission_id,
                    attempt.step_id,
                    final_state="failed",
                    result_kind=StepResultKind.FAILURE.value,
                    result_output={"error": reason},
                    recorded_at=completed,
                )
                self._clear_schedule(attempt.mission_id, attempt.step_id, completed)
                self._connection.execute(
                    "UPDATE missions SET state = 'failed', updated_at = ? WHERE mission_id = ?",
                    (completed, attempt.mission_id),
                )
                self._append_core_journal(
                    mission_id=attempt.mission_id,
                    step_id=None,
                    entity_type="mission",
                    from_state="running",
                    to_state="failed",
                    recorded_at=completed,
                )
                decision = AttemptDecisionKind.FAILED

        persisted = self.latest_attempt(attempt.mission_id, attempt.step_id)
        assert persisted is not None
        return AttemptDecision(kind=decision, attempt=persisted, retry_due_at=retry_due)

    def _finalize_core_step(
        self,
        mission_id: str,
        step_id: str,
        *,
        final_state: str,
        result_kind: str,
        result_output: dict[str, object],
        recorded_at: str,
    ) -> None:
        row = self._connection.execute(
            "SELECT state FROM steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown step: {mission_id}/{step_id}")
        if row["state"] != "pending":
            raise AttemptError(
                f"attempt finalization requires pending core step, got {row['state']}"
            )

        # Preserve the Phase 1 state-machine history while making both transitions
        # atomic at the SQLite boundary. No observer can see the synthetic RUNNING
        # state between PENDING and the terminal state.
        self._append_core_journal(
            mission_id=mission_id,
            step_id=step_id,
            entity_type="step",
            from_state="pending",
            to_state="running",
            recorded_at=recorded_at,
        )
        self._connection.execute(
            """
            UPDATE steps
               SET state = ?, result_schema_version = 1, result_kind = ?,
                   result_json = ?, updated_at = ?
             WHERE mission_id = ? AND step_id = ?
            """,
            (
                final_state,
                result_kind,
                canonical_json(result_output),
                recorded_at,
                mission_id,
                step_id,
            ),
        )
        self._append_core_journal(
            mission_id=mission_id,
            step_id=step_id,
            entity_type="step",
            from_state="running",
            to_state=final_state,
            recorded_at=recorded_at,
        )

    def _upsert_schedule(self, mission_id: str, step_id: str, due_at: str, recorded_at: str) -> None:
        existing = self._connection.execute(
            "SELECT due_at, created_at FROM scheduled_steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        ).fetchone()
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO scheduled_steps(mission_id, step_id, due_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (mission_id, step_id, due_at, recorded_at, recorded_at),
            )
            event_type = "scheduled"
            previous = None
        else:
            self._connection.execute(
                """
                UPDATE scheduled_steps SET due_at = ?, updated_at = ?
                 WHERE mission_id = ? AND step_id = ?
                """,
                (due_at, recorded_at, mission_id, step_id),
            )
            event_type = "rescheduled"
            previous = existing["due_at"]
        self._connection.execute(
            """
            INSERT INTO schedule_events(
                mission_id, step_id, event_type, previous_due_at, due_at, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (mission_id, step_id, event_type, previous, due_at, recorded_at),
        )

    def _clear_schedule(self, mission_id: str, step_id: str, recorded_at: str) -> None:
        existing = self._connection.execute(
            "SELECT due_at FROM scheduled_steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        ).fetchone()
        if existing is None:
            return
        old_due = existing["due_at"]
        self._connection.execute(
            "DELETE FROM scheduled_steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        )
        self._connection.execute(
            """
            INSERT INTO schedule_events(
                mission_id, step_id, event_type, previous_due_at, due_at, recorded_at
            ) VALUES (?, ?, 'cleared', ?, ?, ?)
            """,
            (mission_id, step_id, old_due, old_due, recorded_at),
        )

    def _append_core_journal(
        self,
        *,
        mission_id: str,
        step_id: str | None,
        entity_type: str,
        from_state: str,
        to_state: str,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO journal(
                mission_id, step_id, entity_type, event_type, from_state, to_state, recorded_at
            ) VALUES (?, ?, ?, 'state_transition', ?, ?, ?)
            """,
            (mission_id, step_id, entity_type, from_state, to_state, recorded_at),
        )

    def _append_attempt_event(
        self,
        mission_id: str,
        step_id: str,
        attempt_no: int,
        event_type: str,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO attempt_events(mission_id, step_id, attempt_no, event_type, recorded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (mission_id, step_id, attempt_no, event_type, recorded_at),
        )

    def _initialize_or_validate(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        required = {"missions", "steps", "journal", "scheduled_steps", "schedule_events"}
        if not required.issubset(tables):
            raise AttemptError(
                "AttemptStore requires initialized mission and scheduling state before use"
            )
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_attempt_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts(
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    request_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    outcome TEXT,
                    duration_ms INTEGER,
                    exit_code INTEGER,
                    input_tokens INTEGER,
                    output_tokens INTEGER,
                    cost_usd REAL,
                    retry_due_at TEXT,
                    detail TEXT,
                    PRIMARY KEY(mission_id, step_id, attempt_no),
                    FOREIGN KEY(mission_id, step_id)
                        REFERENCES steps(mission_id, step_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attempt_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(mission_id, step_id, attempt_no)
                        REFERENCES attempts(mission_id, step_id, attempt_no) ON DELETE CASCADE
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM argus_attempt_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO argus_attempt_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_ATTEMPT_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise AttemptError("attempt schema version is invalid") from exc
                if version != _ATTEMPT_SCHEMA_VERSION:
                    raise AttemptError(f"unsupported attempt schema version: {version}")


class AttemptExecutor:
    """Execute exactly one durably due Phase 2 step through a bounded worker."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)

    def execute_due_step(
        self,
        mission_id: str,
        step_id: str,
        worker: WorkerInvoker,
        *,
        policy: AttemptPolicy = AttemptPolicy(),
        now: str | datetime | None = None,
    ) -> AttemptDecision:
        instant = canonical_due_at(now or datetime.now(timezone.utc))
        with DurableScheduler(self.path) as scheduler:
            eligible = {
                (item.mission_id, item.step_id) for item in scheduler.due(instant)
            }
        if (mission_id, step_id) not in eligible:
            raise AttemptNotDueError(
                f"step {mission_id}/{step_id} is not durably eligible at {instant}"
            )

        with AttemptStore(self.path) as store:
            latest = store.latest_attempt(mission_id, step_id)
            if latest is not None and latest.state is AttemptState.STARTED:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has no durable outcome; replay is blocked"
                )
            if latest is not None and latest.outcome is InvocationOutcome.TERMINATION_UNCERTAIN:
                raise AmbiguousAttemptError(
                    f"attempt {latest.request_id} has uncertain termination; replay is blocked"
                )
            attempt = store.begin_attempt(mission_id, step_id, started_at=instant)

        # Read the durable step only after the attempt exists. A crash from this point
        # onward leaves STARTED attempt evidence and therefore blocks blind replay.
        from argus.store import SqliteMissionStore

        with SqliteMissionStore(self.path) as mission_store:
            step = mission_store.load_step(mission_id, step_id)
        request = WorkerRequest(
            request_id=attempt.request_id,
            operation=step.envelope.operation,
            payload=step.envelope.payload,
            payload_version=step.envelope.payload_version,
        )
        invocation = worker.invoke(request)
        completed_at = datetime.now(timezone.utc)
        with AttemptStore(self.path) as store:
            return store.complete_attempt(
                attempt,
                invocation,
                completed_at=completed_at,
                policy=policy,
            )


def _attempt_from_row(row: sqlite3.Row) -> AttemptRecord:
    state = AttemptState(row["state"])
    outcome = InvocationOutcome(row["outcome"]) if row["outcome"] is not None else None
    completed_at = row["completed_at"]
    if state is AttemptState.STARTED and (
        completed_at is not None or outcome is not None or row["duration_ms"] is not None
    ):
        raise AttemptError("started attempt contains terminal fields")
    if state is AttemptState.COMPLETED and (completed_at is None or outcome is None):
        raise AttemptError("completed attempt is missing terminal fields")
    for timestamp in (row["started_at"], completed_at, row["retry_due_at"]):
        if timestamp is not None:
            try:
                if canonical_due_at(timestamp) != timestamp:
                    raise AttemptError("attempt timestamp is not canonical")
            except (TypeError, ValueError) as exc:
                raise AttemptError("attempt contains invalid timestamp") from exc
    return AttemptRecord(
        mission_id=row["mission_id"],
        step_id=row["step_id"],
        attempt_no=row["attempt_no"],
        request_id=row["request_id"],
        state=state,
        started_at=row["started_at"],
        completed_at=completed_at,
        outcome=outcome,
        duration_ms=row["duration_ms"],
        exit_code=row["exit_code"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        cost_usd=row["cost_usd"],
        retry_due_at=row["retry_due_at"],
        detail=row["detail"],
    )


def _is_retryable(invocation: WorkerInvocation, policy: AttemptPolicy) -> bool:
    if invocation.outcome is InvocationOutcome.RETRYABLE_FAILURE:
        return True
    if invocation.outcome is InvocationOutcome.PROCESS_ERROR:
        return policy.retry_process_errors
    if invocation.outcome is InvocationOutcome.TIMEOUT:
        return policy.retry_timeouts
    return False


def _parse_canonical(value: str) -> datetime:
    return datetime.fromisoformat(value[:-1] + "+00:00")


def _bounded_detail(value: str | None, limit: int = 1000) -> str | None:
    if value is None:
        return None
    return value[:limit]
