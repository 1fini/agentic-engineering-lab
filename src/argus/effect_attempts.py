"""Durable Phase 3 effect-attempt lineage and safe re-attempt policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
import sqlite3

from argus.effects import (
    AmbiguousEffectError,
    EffectError,
    EffectReceiptOutcome,
    EffectReceiptSource,
    EffectRecord,
    EffectState,
    EffectStore,
)
from argus.scheduler import canonical_due_at

_EFFECT_ATTEMPT_SCHEMA_VERSION = 1


class EffectAttemptError(EffectError):
    """Base error for durable effect-attempt lineage."""


class EffectAttemptLimitError(EffectAttemptError):
    """Raised when policy forbids another external effect attempt."""


class EffectAttemptState(StrEnum):
    OUTCOME_UNKNOWN = "outcome_unknown"
    CONFIRMED_APPLIED = "confirmed_applied"
    CONFIRMED_NOT_APPLIED = "confirmed_not_applied"


@dataclass(frozen=True)
class EffectReplayPolicy:
    max_effect_attempts: int = 2

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_effect_attempts, int)
            or isinstance(self.max_effect_attempts, bool)
            or self.max_effect_attempts < 1
        ):
            raise ValueError("max_effect_attempts must be a positive integer")


@dataclass(frozen=True)
class EffectAttemptRecord:
    mission_id: str
    step_id: str
    effect_id: str
    attempt_no: int
    attempt_key: str
    state: EffectAttemptState
    started_at: str
    resolved_at: str | None
    receipt_outcome: EffectReceiptOutcome | None
    receipt_source: EffectReceiptSource | None


class EffectAttemptStore:
    """Persist effect-attempt lineage separately from Phase 2 worker attempts."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise EffectAttemptError("EffectAttemptStore requires file-backed ARGUS state")
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
            self._repair_from_parent_state()
        except EffectAttemptError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise EffectAttemptError(f"invalid ARGUS effect-attempt store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EffectAttemptStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def list_attempts(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
    ) -> list[EffectAttemptRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM effect_attempts
             WHERE mission_id = ? AND step_id = ? AND effect_id = ?
             ORDER BY attempt_no ASC
            """,
            (mission_id, step_id, effect_id),
        ).fetchall()
        return [self._record_from_row(row) for row in rows]

    def latest_attempt(
        self,
        mission_id: str,
        step_id: str,
        effect_id: str,
    ) -> EffectAttemptRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM effect_attempts
             WHERE mission_id = ? AND step_id = ? AND effect_id = ?
             ORDER BY attempt_no DESC LIMIT 1
            """,
            (mission_id, step_id, effect_id),
        ).fetchone()
        return self._record_from_row(row) if row is not None else None

    def ensure_current_attempt(self, effect: EffectRecord) -> EffectAttemptRecord:
        self._repair_one(effect.intent.mission_id, effect.intent.step_id, effect.intent.effect_id)
        latest = self.latest_attempt(
            effect.intent.mission_id,
            effect.intent.step_id,
            effect.intent.effect_id,
        )
        if latest is None:
            raise EffectAttemptError("effect has no durable execution attempt")
        return latest

    def synchronize_terminal_effect(self, effect: EffectRecord) -> EffectAttemptRecord:
        if effect.state not in {
            EffectState.CONFIRMED_APPLIED,
            EffectState.CONFIRMED_NOT_APPLIED,
        } or effect.receipt is None:
            raise EffectAttemptError("only a confirmed effect can resolve an effect attempt")

        with self._connection:
            latest = self.latest_attempt(
                effect.intent.mission_id,
                effect.intent.step_id,
                effect.intent.effect_id,
            )
            if latest is None:
                raise EffectAttemptError("confirmed effect has no durable effect attempt")
            target = (
                EffectAttemptState.CONFIRMED_APPLIED
                if effect.state is EffectState.CONFIRMED_APPLIED
                else EffectAttemptState.CONFIRMED_NOT_APPLIED
            )
            if latest.state is target:
                return latest
            if latest.state is not EffectAttemptState.OUTCOME_UNKNOWN:
                raise EffectAttemptError(
                    f"cannot resolve effect attempt from {latest.state.value}"
                )
            self._connection.execute(
                """
                UPDATE effect_attempts
                   SET state = ?, resolved_at = ?, receipt_outcome = ?, receipt_source = ?
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ? AND attempt_no = ?
                """,
                (
                    target.value,
                    effect.updated_at,
                    effect.receipt.outcome.value,
                    effect.receipt.source.value,
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                    latest.attempt_no,
                ),
            )
        persisted = self.latest_attempt(
            effect.intent.mission_id,
            effect.intent.step_id,
            effect.intent.effect_id,
        )
        assert persisted is not None
        return persisted

    def authorize_reattempt(
        self,
        effect: EffectRecord,
        *,
        policy: EffectReplayPolicy,
        recorded_at: str | datetime | None = None,
    ) -> EffectAttemptRecord:
        if effect.state is EffectState.OUTCOME_UNKNOWN:
            raise AmbiguousEffectError(
                f"effect {effect.intent.correlation_key} is still ambiguous"
            )
        if effect.state is EffectState.CONFIRMED_APPLIED:
            raise EffectAttemptError("applied effect can never be re-attempted")
        if effect.state is not EffectState.CONFIRMED_NOT_APPLIED:
            raise EffectAttemptError(
                f"re-attempt requires confirmed_not_applied, got {effect.state.value}"
            )

        timestamp = _canonical_time(recorded_at)
        with self._connection:
            durable = self._connection.execute(
                """
                SELECT state, correlation_key FROM effects
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                ),
            ).fetchone()
            if durable is None:
                raise KeyError(
                    f"unknown effect: {effect.intent.mission_id}/{effect.intent.step_id}/{effect.intent.effect_id}"
                )
            if durable["state"] != EffectState.CONFIRMED_NOT_APPLIED.value:
                if durable["state"] == EffectState.OUTCOME_UNKNOWN.value:
                    raise AmbiguousEffectError(
                        f"effect {effect.intent.correlation_key} became ambiguous"
                    )
                raise EffectAttemptError(
                    f"durable effect cannot be re-attempted from {durable['state']}"
                )

            attempts = self._connection.execute(
                """
                SELECT COUNT(*) FROM effect_attempts
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                ),
            ).fetchone()[0]
            if attempts >= policy.max_effect_attempts:
                raise EffectAttemptLimitError(
                    f"effect attempt limit exhausted: {attempts}/{policy.max_effect_attempts}"
                )
            attempt_no = attempts + 1
            attempt_key = derive_effect_attempt_key(
                effect.intent.correlation_key or "",
                attempt_no,
            )
            self._connection.execute(
                """
                INSERT INTO effect_attempts(
                    mission_id, step_id, effect_id, attempt_no, attempt_key, state,
                    started_at, resolved_at, receipt_outcome, receipt_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
                """,
                (
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                    attempt_no,
                    attempt_key,
                    EffectAttemptState.OUTCOME_UNKNOWN.value,
                    timestamp,
                ),
            )
            self._connection.execute(
                """
                UPDATE effects
                   SET state = ?, receipt_schema_version = NULL, receipt_outcome = NULL,
                       receipt_source = NULL, receipt_json = NULL, updated_at = ?
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (
                    EffectState.OUTCOME_UNKNOWN.value,
                    timestamp,
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO effect_events(
                    mission_id, step_id, effect_id, event_type,
                    from_state, to_state, recorded_at
                ) VALUES (?, ?, ?, 'reattempt_authorized', ?, ?, ?)
                """,
                (
                    effect.intent.mission_id,
                    effect.intent.step_id,
                    effect.intent.effect_id,
                    EffectState.CONFIRMED_NOT_APPLIED.value,
                    EffectState.OUTCOME_UNKNOWN.value,
                    timestamp,
                ),
            )
        latest = self.latest_attempt(
            effect.intent.mission_id,
            effect.intent.step_id,
            effect.intent.effect_id,
        )
        assert latest is not None
        return latest

    def _initialize_or_validate(self) -> None:
        tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"effects", "effect_events"}.issubset(tables):
            raise EffectAttemptError("EffectAttemptStore requires initialized effect state")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_effect_attempt_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS effect_attempts(
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    effect_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    attempt_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    resolved_at TEXT,
                    receipt_outcome TEXT,
                    receipt_source TEXT,
                    PRIMARY KEY(mission_id, step_id, effect_id, attempt_no),
                    FOREIGN KEY(mission_id, step_id, effect_id)
                        REFERENCES effects(mission_id, step_id, effect_id) ON DELETE CASCADE
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM argus_effect_attempt_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO argus_effect_attempt_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_EFFECT_ATTEMPT_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise EffectAttemptError("effect-attempt schema version is invalid") from exc
                if version != _EFFECT_ATTEMPT_SCHEMA_VERSION:
                    raise EffectAttemptError(
                        f"unsupported effect-attempt schema version: {version}"
                    )

    def _repair_from_parent_state(self) -> None:
        rows = self._connection.execute(
            "SELECT mission_id, step_id, effect_id FROM effects"
        ).fetchall()
        for row in rows:
            self._repair_one(row["mission_id"], row["step_id"], row["effect_id"])

    def _repair_one(self, mission_id: str, step_id: str, effect_id: str) -> None:
        with self._connection:
            parent = self._connection.execute(
                """
                SELECT * FROM effects
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                """,
                (mission_id, step_id, effect_id),
            ).fetchone()
            if parent is None:
                raise KeyError(f"unknown effect: {mission_id}/{step_id}/{effect_id}")
            attempts = self._connection.execute(
                """
                SELECT * FROM effect_attempts
                 WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                 ORDER BY attempt_no ASC
                """,
                (mission_id, step_id, effect_id),
            ).fetchall()
            if not attempts:
                boundary = self._connection.execute(
                    """
                    SELECT recorded_at FROM effect_events
                     WHERE mission_id = ? AND step_id = ? AND effect_id = ?
                       AND event_type = 'execution_boundary_entered'
                     ORDER BY sequence ASC LIMIT 1
                    """,
                    (mission_id, step_id, effect_id),
                ).fetchone()
                if boundary is None:
                    return
                state = _attempt_state_from_parent(parent["state"])
                terminal = state is not EffectAttemptState.OUTCOME_UNKNOWN
                self._connection.execute(
                    """
                    INSERT INTO effect_attempts(
                        mission_id, step_id, effect_id, attempt_no, attempt_key, state,
                        started_at, resolved_at, receipt_outcome, receipt_source
                    ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mission_id,
                        step_id,
                        effect_id,
                        derive_effect_attempt_key(parent["correlation_key"], 1),
                        state.value,
                        boundary["recorded_at"],
                        parent["updated_at"] if terminal else None,
                        parent["receipt_outcome"] if terminal else None,
                        parent["receipt_source"] if terminal else None,
                    ),
                )
                return

            latest = attempts[-1]
            parent_state = EffectState(parent["state"])
            if (
                latest["state"] == EffectAttemptState.OUTCOME_UNKNOWN.value
                and parent_state
                in {EffectState.CONFIRMED_APPLIED, EffectState.CONFIRMED_NOT_APPLIED}
            ):
                target = (
                    EffectAttemptState.CONFIRMED_APPLIED
                    if parent_state is EffectState.CONFIRMED_APPLIED
                    else EffectAttemptState.CONFIRMED_NOT_APPLIED
                )
                self._connection.execute(
                    """
                    UPDATE effect_attempts
                       SET state = ?, resolved_at = ?, receipt_outcome = ?, receipt_source = ?
                     WHERE mission_id = ? AND step_id = ? AND effect_id = ? AND attempt_no = ?
                    """,
                    (
                        target.value,
                        parent["updated_at"],
                        parent["receipt_outcome"],
                        parent["receipt_source"],
                        mission_id,
                        step_id,
                        effect_id,
                        latest["attempt_no"],
                    ),
                )

    def _record_from_row(self, row: sqlite3.Row) -> EffectAttemptRecord:
        try:
            state = EffectAttemptState(row["state"])
            outcome = (
                EffectReceiptOutcome(row["receipt_outcome"])
                if row["receipt_outcome"] is not None
                else None
            )
            source = (
                EffectReceiptSource(row["receipt_source"])
                if row["receipt_source"] is not None
                else None
            )
        except ValueError as exc:
            raise EffectAttemptError("effect-attempt durable state is invalid") from exc
        started_at = _validated_time(row["started_at"])
        resolved_at = (
            _validated_time(row["resolved_at"])
            if row["resolved_at"] is not None
            else None
        )
        if state is EffectAttemptState.OUTCOME_UNKNOWN:
            if resolved_at is not None or outcome is not None or source is not None:
                raise EffectAttemptError("unknown effect attempt contains terminal receipt fields")
        else:
            if resolved_at is None or outcome is None or source is None:
                raise EffectAttemptError("confirmed effect attempt is missing receipt fields")
            expected = (
                EffectReceiptOutcome.APPLIED
                if state is EffectAttemptState.CONFIRMED_APPLIED
                else EffectReceiptOutcome.NOT_APPLIED
            )
            if outcome is not expected:
                raise EffectAttemptError("effect-attempt state and receipt outcome disagree")
        return EffectAttemptRecord(
            mission_id=row["mission_id"],
            step_id=row["step_id"],
            effect_id=row["effect_id"],
            attempt_no=row["attempt_no"],
            attempt_key=row["attempt_key"],
            state=state,
            started_at=started_at,
            resolved_at=resolved_at,
            receipt_outcome=outcome,
            receipt_source=source,
        )


def derive_effect_attempt_key(correlation_key: str, attempt_no: int) -> str:
    if not correlation_key:
        raise ValueError("correlation_key must be non-empty")
    if not isinstance(attempt_no, int) or isinstance(attempt_no, bool) or attempt_no < 1:
        raise ValueError("attempt_no must be a positive integer")
    return f"{correlation_key}:attempt:{attempt_no}"


def _attempt_state_from_parent(parent_state: str) -> EffectAttemptState:
    state = EffectState(parent_state)
    if state is EffectState.OUTCOME_UNKNOWN:
        return EffectAttemptState.OUTCOME_UNKNOWN
    if state is EffectState.CONFIRMED_APPLIED:
        return EffectAttemptState.CONFIRMED_APPLIED
    if state is EffectState.CONFIRMED_NOT_APPLIED:
        return EffectAttemptState.CONFIRMED_NOT_APPLIED
    raise EffectAttemptError("intent-only effect has no execution attempt")


def _canonical_time(value: str | datetime | None) -> str:
    if value is None:
        value = datetime.now(timezone.utc)
    return canonical_due_at(value)


def _validated_time(value: str) -> str:
    try:
        canonical = canonical_due_at(value)
    except (TypeError, ValueError) as exc:
        raise EffectAttemptError("effect-attempt timestamp is invalid") from exc
    if canonical != value:
        raise EffectAttemptError("effect-attempt timestamp is not canonical")
    return value
