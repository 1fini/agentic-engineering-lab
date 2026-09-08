"""Durable Phase 2 scheduling primitives for ARGUS.

The scheduler is intentionally a deterministic read/write boundary over persisted
eligibility. It does not execute workers and it does not create execution attempts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

from argus.model import ArgusStateError

_SCHEDULE_SCHEMA_VERSION = 1


class ScheduleError(ArgusStateError):
    """Raised when durable scheduling state is invalid or cannot be applied safely."""


@dataclass(frozen=True)
class ScheduledStep:
    mission_id: str
    step_id: str
    due_at: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ScheduleEvent:
    sequence: int
    mission_id: str
    step_id: str
    event_type: str
    previous_due_at: str | None
    due_at: str
    recorded_at: str


class SystemClock:
    """UTC wall clock used only at deterministic scheduler boundaries."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class DurableScheduler:
    """Persist and query future step eligibility in the ARGUS SQLite state file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise ScheduleError(
                "DurableScheduler requires a file-backed ARGUS store; :memory: cannot be shared"
            )
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
        except ScheduleError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise ScheduleError(f"invalid ARGUS scheduler store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DurableScheduler":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def schedule(self, mission_id: str, step_id: str, due_at: str | datetime) -> ScheduledStep:
        try:
            canonical_due = canonical_due_at(due_at)
        except (TypeError, ValueError) as exc:
            raise ScheduleError(str(exc)) from exc
        step = self._connection.execute(
            "SELECT state FROM steps WHERE mission_id = ? AND step_id = ?",
            (mission_id, step_id),
        ).fetchone()
        if step is None:
            raise KeyError(f"unknown step: {mission_id}/{step_id}")
        if step["state"] != "pending":
            raise ScheduleError(
                f"only pending steps may be scheduled: {mission_id}/{step_id} is {step['state']}"
            )

        existing = self.get(mission_id, step_id)
        if existing is not None and existing.due_at == canonical_due:
            return existing

        now = canonical_due_at(datetime.now(timezone.utc))
        with self._connection:
            if existing is None:
                self._connection.execute(
                    """
                    INSERT INTO scheduled_steps(mission_id, step_id, due_at, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (mission_id, step_id, canonical_due, now, now),
                )
                event_type = "scheduled"
                previous_due = None
            else:
                self._connection.execute(
                    """
                    UPDATE scheduled_steps
                       SET due_at = ?, updated_at = ?
                     WHERE mission_id = ? AND step_id = ?
                    """,
                    (canonical_due, now, mission_id, step_id),
                )
                event_type = "rescheduled"
                previous_due = existing.due_at
            self._connection.execute(
                """
                INSERT INTO schedule_events(
                    mission_id, step_id, event_type, previous_due_at, due_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mission_id, step_id, event_type, previous_due, canonical_due, now),
            )
        scheduled = self.get(mission_id, step_id)
        assert scheduled is not None
        return scheduled

    def get(self, mission_id: str, step_id: str) -> ScheduledStep | None:
        row = self._connection.execute(
            """
            SELECT mission_id, step_id, due_at, created_at, updated_at
              FROM scheduled_steps
             WHERE mission_id = ? AND step_id = ?
            """,
            (mission_id, step_id),
        ).fetchone()
        return _scheduled_from_row(row) if row is not None else None

    def list_for_mission(self, mission_id: str) -> list[ScheduledStep]:
        rows = self._connection.execute(
            """
            SELECT ss.mission_id, ss.step_id, ss.due_at, ss.created_at, ss.updated_at
              FROM scheduled_steps ss
              JOIN steps s
                ON s.mission_id = ss.mission_id AND s.step_id = ss.step_id
             WHERE ss.mission_id = ?
             ORDER BY s.ordinal ASC
            """,
            (mission_id,),
        ).fetchall()
        return [_scheduled_from_row(row) for row in rows]

    def due(self, now: str | datetime | None = None) -> list[ScheduledStep]:
        try:
            instant = canonical_due_at(now or datetime.now(timezone.utc))
        except (TypeError, ValueError) as exc:
            raise ScheduleError(str(exc)) from exc
        rows = self._connection.execute(
            """
            SELECT ss.mission_id, ss.step_id, ss.due_at, ss.created_at, ss.updated_at
              FROM scheduled_steps ss
              JOIN steps s
                ON s.mission_id = ss.mission_id AND s.step_id = ss.step_id
              JOIN missions m
                ON m.mission_id = ss.mission_id
             WHERE ss.due_at <= ?
               AND s.state = 'pending'
               AND m.state IN ('pending', 'running')
               AND NOT EXISTS (
                    SELECT 1
                      FROM steps prior
                     WHERE prior.mission_id = s.mission_id
                       AND prior.ordinal < s.ordinal
                       AND prior.state != 'succeeded'
               )
             ORDER BY ss.due_at ASC, s.ordinal ASC, ss.mission_id ASC, ss.step_id ASC
            """,
            (instant,),
        ).fetchall()
        return [_scheduled_from_row(row) for row in rows]

    def history(self, mission_id: str, step_id: str | None = None) -> list[ScheduleEvent]:
        if step_id is None:
            rows = self._connection.execute(
                """
                SELECT * FROM schedule_events
                 WHERE mission_id = ?
                 ORDER BY sequence ASC
                """,
                (mission_id,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM schedule_events
                 WHERE mission_id = ? AND step_id = ?
                 ORDER BY sequence ASC
                """,
                (mission_id, step_id),
            ).fetchall()
        return [
            ScheduleEvent(
                sequence=row["sequence"],
                mission_id=row["mission_id"],
                step_id=row["step_id"],
                event_type=row["event_type"],
                previous_due_at=row["previous_due_at"],
                due_at=row["due_at"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def _initialize_or_validate(self) -> None:
        base_tables = {
            row[0]
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if not {"missions", "steps", "argus_metadata"}.issubset(base_tables):
            raise ScheduleError(
                "scheduler requires an initialized ARGUS mission store before scheduling"
            )

        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_schedule_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_steps (
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    due_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (mission_id, step_id),
                    FOREIGN KEY (mission_id, step_id)
                        REFERENCES steps(mission_id, step_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schedule_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    step_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    previous_due_at TEXT,
                    due_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY (mission_id, step_id)
                        REFERENCES steps(mission_id, step_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS scheduled_steps_due_idx ON scheduled_steps(due_at)"
            )
            existing = self._connection.execute(
                "SELECT value FROM argus_schedule_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO argus_schedule_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_SCHEDULE_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(existing[0])
                except (TypeError, ValueError) as exc:
                    raise ScheduleError("scheduler schema version is invalid") from exc
                if version != _SCHEDULE_SCHEMA_VERSION:
                    raise ScheduleError(f"unsupported scheduler schema version: {version}")

        rows = self._connection.execute(
            "SELECT mission_id, step_id, due_at FROM scheduled_steps"
        ).fetchall()
        for row in rows:
            try:
                canonical = canonical_due_at(row["due_at"])
            except ValueError as exc:
                raise ScheduleError(
                    f"invalid persisted due_at for {row['mission_id']}/{row['step_id']}"
                ) from exc
            if canonical != row["due_at"]:
                raise ScheduleError(
                    f"non-canonical persisted due_at for {row['mission_id']}/{row['step_id']}"
                )


def canonical_due_at(value: str | datetime) -> str:
    """Return a canonical UTC ISO-8601 timestamp with microseconds and `Z`."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("due_at must be a non-empty ISO-8601 timestamp")
        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            instant = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"invalid due_at timestamp: {value!r}") from exc
    elif isinstance(value, datetime):
        instant = value
    else:
        raise TypeError("due_at must be an ISO-8601 string or datetime")

    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("due_at must include an explicit timezone offset")
    instant = instant.astimezone(timezone.utc)
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _scheduled_from_row(row: sqlite3.Row) -> ScheduledStep:
    try:
        due_at = canonical_due_at(row["due_at"])
    except ValueError as exc:
        raise ScheduleError(
            f"invalid persisted due_at for {row['mission_id']}/{row['step_id']}"
        ) from exc
    if due_at != row["due_at"]:
        raise ScheduleError(
            f"non-canonical persisted due_at for {row['mission_id']}/{row['step_id']}"
        )
    return ScheduledStep(
        mission_id=row["mission_id"],
        step_id=row["step_id"],
        due_at=due_at,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
