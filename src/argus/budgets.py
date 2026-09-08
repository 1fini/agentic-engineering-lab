"""Durable Phase 4 attempt/spend budgets and crash-safe reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
import hashlib
import json
from pathlib import Path
import sqlite3

from argus.model import ArgusStateError

_BUDGET_SCHEMA_VERSION = 1


class BudgetError(ArgusStateError):
    """Raised when durable budget state is invalid or a budget action is unsafe."""


class BudgetExhaustedError(BudgetError):
    """Raised when a reservation/allocation would exceed a configured budget."""


class BudgetDecisionKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


class BudgetReason(StrEnum):
    ALLOWED = "allowed"
    WORKER_ATTEMPT_EXHAUSTED = "worker_attempt_exhausted"
    EFFECT_ATTEMPT_EXHAUSTED = "effect_attempt_exhausted"
    SPEND_EXHAUSTED = "spend_exhausted"


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(frozen=True)
class BudgetPolicy:
    mission_id: str
    policy_version: int = 1
    worker_attempt_limit: int | None = None
    effect_attempt_limit: int | None = None
    spend_limit_usd: Decimal | str | int | float | None = None
    schema_version: int = _BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.mission_id:
            raise ValueError("mission_id must be non-empty")
        if self.policy_version < 1:
            raise ValueError("policy_version must be >= 1")
        if self.schema_version != _BUDGET_SCHEMA_VERSION:
            raise BudgetError(
                f"unsupported budget policy schema version: {self.schema_version}"
            )
        for name, value in (
            ("worker_attempt_limit", self.worker_attempt_limit),
            ("effect_attempt_limit", self.effect_attempt_limit),
        ):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if self.spend_limit_usd is not None:
            object.__setattr__(
                self, "spend_limit_usd", _money(self.spend_limit_usd, allow_zero=True)
            )

    @property
    def policy_hash(self) -> str:
        material = {
            "schema_version": self.schema_version,
            "mission_id": self.mission_id,
            "policy_version": self.policy_version,
            "worker_attempt_limit": self.worker_attempt_limit,
            "effect_attempt_limit": self.effect_attempt_limit,
            "spend_limit_usd": (
                _money_text(self.spend_limit_usd)
                if self.spend_limit_usd is not None
                else None
            ),
        }
        encoded = json.dumps(
            material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return f"argus:budget:v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True)
class BudgetSnapshot:
    policy: BudgetPolicy
    worker_attempts_used: int
    effect_attempts_used: int
    spend_committed_usd: Decimal
    spend_reserved_usd: Decimal

    @property
    def spend_available_usd(self) -> Decimal | None:
        if self.policy.spend_limit_usd is None:
            return None
        return max(
            Decimal("0"),
            self.policy.spend_limit_usd
            - self.spend_committed_usd
            - self.spend_reserved_usd,
        )


@dataclass(frozen=True)
class BudgetDecision:
    mission_id: str
    kind: BudgetDecisionKind
    reason: BudgetReason
    policy_version: int
    policy_hash: str
    used: Decimal
    limit: Decimal | None
    requested: Decimal | None = None


@dataclass(frozen=True)
class SpendReservation:
    reservation_key: str
    mission_id: str
    state: ReservationState
    reserved_usd: Decimal
    committed_usd: Decimal | None
    policy_version: int
    policy_hash: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class BudgetEvent:
    sequence: int
    mission_id: str
    event_type: str
    reservation_key: str | None
    amount_usd: Decimal | None
    reason: str | None
    policy_version: int
    policy_hash: str
    recorded_at: str


class BudgetStore:
    """Persist generic attempt limits and crash-safe spend reservations."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path == ":memory:":
            raise BudgetError("BudgetStore requires file-backed ARGUS state")
        try:
            self._connection = sqlite3.connect(self.path)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._initialize_or_validate()
        except BudgetError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.DatabaseError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise BudgetError(f"invalid ARGUS budget store: {self.path}") from exc

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "BudgetStore":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def set_policy(self, policy: BudgetPolicy) -> BudgetPolicy:
        self._require_mission(policy.mission_id)
        existing = self.get_policy(policy.mission_id)
        if existing is not None:
            if existing == policy:
                return existing
            if policy.policy_version <= existing.policy_version:
                raise BudgetError(
                    "budget policy updates require a strictly increasing policy_version"
                )
            event_type = "budget_policy_updated"
        else:
            event_type = "budget_policy_created"
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO budget_policies(
                    mission_id, policy_version, worker_attempt_limit,
                    effect_attempt_limit, spend_limit_usd, policy_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(mission_id) DO UPDATE SET
                    policy_version = excluded.policy_version,
                    worker_attempt_limit = excluded.worker_attempt_limit,
                    effect_attempt_limit = excluded.effect_attempt_limit,
                    spend_limit_usd = excluded.spend_limit_usd,
                    policy_hash = excluded.policy_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.mission_id,
                    policy.policy_version,
                    policy.worker_attempt_limit,
                    policy.effect_attempt_limit,
                    _money_text(policy.spend_limit_usd)
                    if policy.spend_limit_usd is not None
                    else None,
                    policy.policy_hash,
                    now,
                ),
            )
            self._append_event(
                mission_id=policy.mission_id,
                event_type=event_type,
                reservation_key=None,
                amount_usd=None,
                reason=None,
                policy=policy,
                recorded_at=now,
            )
        return self.load_policy(policy.mission_id)

    def get_policy(self, mission_id: str) -> BudgetPolicy | None:
        row = self._connection.execute(
            "SELECT * FROM budget_policies WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        return self._policy_from_row(row) if row is not None else None

    def load_policy(self, mission_id: str) -> BudgetPolicy:
        policy = self.get_policy(mission_id)
        if policy is None:
            raise KeyError(f"budget policy is not initialized: {mission_id}")
        return policy

    def snapshot(self, mission_id: str) -> BudgetSnapshot:
        policy = self.load_policy(mission_id)
        worker_used = self._attempt_count("attempts", mission_id)
        effect_used = self._attempt_count("effect_attempts", mission_id)
        rows = self._connection.execute(
            "SELECT state, reserved_usd, committed_usd FROM spend_reservations WHERE mission_id = ?",
            (mission_id,),
        ).fetchall()
        committed = Decimal("0")
        reserved = Decimal("0")
        for row in rows:
            state = ReservationState(row["state"])
            if state is ReservationState.COMMITTED:
                committed += _money(row["committed_usd"], allow_zero=True)
            elif state is ReservationState.RESERVED:
                reserved += _money(row["reserved_usd"], allow_zero=True)
        return BudgetSnapshot(
            policy=policy,
            worker_attempts_used=worker_used,
            effect_attempts_used=effect_used,
            spend_committed_usd=committed,
            spend_reserved_usd=reserved,
        )

    def check_worker_attempt(self, mission_id: str) -> BudgetDecision:
        snapshot = self.snapshot(mission_id)
        limit = snapshot.policy.worker_attempt_limit
        used = snapshot.worker_attempts_used
        allowed = limit is None or used < limit
        return BudgetDecision(
            mission_id=mission_id,
            kind=BudgetDecisionKind.ALLOW if allowed else BudgetDecisionKind.DENY,
            reason=BudgetReason.ALLOWED if allowed else BudgetReason.WORKER_ATTEMPT_EXHAUSTED,
            policy_version=snapshot.policy.policy_version,
            policy_hash=snapshot.policy.policy_hash,
            used=Decimal(used),
            limit=Decimal(limit) if limit is not None else None,
        )

    def check_effect_attempt(self, mission_id: str) -> BudgetDecision:
        snapshot = self.snapshot(mission_id)
        limit = snapshot.policy.effect_attempt_limit
        used = snapshot.effect_attempts_used
        allowed = limit is None or used < limit
        return BudgetDecision(
            mission_id=mission_id,
            kind=BudgetDecisionKind.ALLOW if allowed else BudgetDecisionKind.DENY,
            reason=BudgetReason.ALLOWED if allowed else BudgetReason.EFFECT_ATTEMPT_EXHAUSTED,
            policy_version=snapshot.policy.policy_version,
            policy_hash=snapshot.policy.policy_hash,
            used=Decimal(used),
            limit=Decimal(limit) if limit is not None else None,
        )

    def check_spend(
        self, mission_id: str, amount_usd: Decimal | str | int | float
    ) -> BudgetDecision:
        amount = _money(amount_usd)
        snapshot = self.snapshot(mission_id)
        limit = snapshot.policy.spend_limit_usd
        used = snapshot.spend_committed_usd + snapshot.spend_reserved_usd
        allowed = limit is None or used + amount <= limit
        return BudgetDecision(
            mission_id=mission_id,
            kind=BudgetDecisionKind.ALLOW if allowed else BudgetDecisionKind.DENY,
            reason=BudgetReason.ALLOWED if allowed else BudgetReason.SPEND_EXHAUSTED,
            policy_version=snapshot.policy.policy_version,
            policy_hash=snapshot.policy.policy_hash,
            used=used,
            limit=limit,
            requested=amount,
        )

    def reserve_spend(
        self,
        mission_id: str,
        reservation_key: str,
        amount_usd: Decimal | str | int | float,
    ) -> SpendReservation:
        if not reservation_key:
            raise ValueError("reservation_key must be non-empty")
        amount = _money(amount_usd)
        existing = self.get_reservation(reservation_key)
        if existing is not None:
            if existing.mission_id == mission_id and existing.reserved_usd == amount:
                return existing
            raise BudgetError("reservation_key already exists with different durable semantics")

        decision = self.check_spend(mission_id, amount)
        if decision.kind is BudgetDecisionKind.DENY:
            raise BudgetExhaustedError(
                f"spend budget exhausted: used={_money_text(decision.used)} "
                f"requested={_money_text(amount)} limit={_money_text(decision.limit)}"
            )
        policy = self.load_policy(mission_id)
        now = _utc_now()
        with self._connection:
            # Re-evaluate inside the write transaction so a second reservation in this
            # single-process SQLite model cannot race the decision made above.
            snapshot = self.snapshot(mission_id)
            if (
                snapshot.policy.spend_limit_usd is not None
                and snapshot.spend_committed_usd
                + snapshot.spend_reserved_usd
                + amount
                > snapshot.policy.spend_limit_usd
            ):
                raise BudgetExhaustedError("spend budget exhausted during reservation")
            self._connection.execute(
                """
                INSERT INTO spend_reservations(
                    reservation_key, mission_id, state, reserved_usd, committed_usd,
                    policy_version, policy_hash, created_at, updated_at
                ) VALUES (?, ?, 'reserved', ?, NULL, ?, ?, ?, ?)
                """,
                (
                    reservation_key,
                    mission_id,
                    _money_text(amount),
                    policy.policy_version,
                    policy.policy_hash,
                    now,
                    now,
                ),
            )
            self._append_event(
                mission_id=mission_id,
                event_type="spend_reserved",
                reservation_key=reservation_key,
                amount_usd=amount,
                reason=None,
                policy=policy,
                recorded_at=now,
            )
        reserved = self.get_reservation(reservation_key)
        assert reserved is not None
        return reserved

    def commit_spend(
        self,
        reservation_key: str,
        actual_usd: Decimal | str | int | float,
    ) -> SpendReservation:
        actual = _money(actual_usd, allow_zero=True)
        current = self.load_reservation(reservation_key)
        if current.state is ReservationState.COMMITTED:
            if current.committed_usd == actual:
                return current
            raise BudgetError("committed reservation cannot change actual spend")
        if current.state is ReservationState.RELEASED:
            raise BudgetError("released reservation cannot be committed")
        if actual > current.reserved_usd:
            raise BudgetError("actual spend cannot exceed durable reservation")
        policy = self.load_policy(current.mission_id)
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                UPDATE spend_reservations
                   SET state = 'committed', committed_usd = ?, updated_at = ?
                 WHERE reservation_key = ? AND state = 'reserved'
                """,
                (_money_text(actual), now, reservation_key),
            )
            self._append_event(
                mission_id=current.mission_id,
                event_type="spend_committed",
                reservation_key=reservation_key,
                amount_usd=actual,
                reason=None,
                policy=policy,
                recorded_at=now,
            )
        return self.load_reservation(reservation_key)

    def release_spend(self, reservation_key: str) -> SpendReservation:
        current = self.load_reservation(reservation_key)
        if current.state is ReservationState.RELEASED:
            return current
        if current.state is ReservationState.COMMITTED:
            raise BudgetError("committed reservation cannot be released")
        policy = self.load_policy(current.mission_id)
        now = _utc_now()
        with self._connection:
            self._connection.execute(
                """
                UPDATE spend_reservations
                   SET state = 'released', updated_at = ?
                 WHERE reservation_key = ? AND state = 'reserved'
                """,
                (now, reservation_key),
            )
            self._append_event(
                mission_id=current.mission_id,
                event_type="spend_released",
                reservation_key=reservation_key,
                amount_usd=current.reserved_usd,
                reason=None,
                policy=policy,
                recorded_at=now,
            )
        return self.load_reservation(reservation_key)

    def get_reservation(self, reservation_key: str) -> SpendReservation | None:
        row = self._connection.execute(
            "SELECT * FROM spend_reservations WHERE reservation_key = ?",
            (reservation_key,),
        ).fetchone()
        return self._reservation_from_row(row) if row is not None else None

    def load_reservation(self, reservation_key: str) -> SpendReservation:
        reservation = self.get_reservation(reservation_key)
        if reservation is None:
            raise KeyError(f"unknown spend reservation: {reservation_key}")
        return reservation

    def list_reservations(self, mission_id: str) -> list[SpendReservation]:
        rows = self._connection.execute(
            "SELECT * FROM spend_reservations WHERE mission_id = ? ORDER BY created_at, reservation_key",
            (mission_id,),
        ).fetchall()
        return [self._reservation_from_row(row) for row in rows]

    def history(self, mission_id: str) -> list[BudgetEvent]:
        rows = self._connection.execute(
            "SELECT * FROM budget_events WHERE mission_id = ? ORDER BY sequence ASC",
            (mission_id,),
        ).fetchall()
        return [
            BudgetEvent(
                sequence=row["sequence"],
                mission_id=row["mission_id"],
                event_type=row["event_type"],
                reservation_key=row["reservation_key"],
                amount_usd=(
                    _money(row["amount_usd"], allow_zero=True)
                    if row["amount_usd"] is not None
                    else None
                ),
                reason=row["reason"],
                policy_version=int(row["policy_version"]),
                policy_hash=row["policy_hash"],
                recorded_at=row["recorded_at"],
            )
            for row in rows
        ]

    def _attempt_count(self, table: str, mission_id: str) -> int:
        if table not in {"attempts", "effect_attempts"}:
            raise AssertionError("unsupported attempt table")
        present = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if present is None:
            return 0
        return int(
            self._connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE mission_id = ?", (mission_id,)
            ).fetchone()[0]
        )

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
            raise BudgetError("budgets require an initialized ARGUS mission store")
        with self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS argus_budget_metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_policies(
                    mission_id TEXT PRIMARY KEY,
                    policy_version INTEGER NOT NULL,
                    worker_attempt_limit INTEGER,
                    effect_attempt_limit INTEGER,
                    spend_limit_usd TEXT,
                    policy_hash TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spend_reservations(
                    reservation_key TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    reserved_usd TEXT NOT NULL,
                    committed_usd TEXT,
                    policy_version INTEGER NOT NULL,
                    policy_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS budget_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    mission_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    reservation_key TEXT,
                    amount_usd TEXT,
                    reason TEXT,
                    policy_version INTEGER NOT NULL,
                    policy_hash TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(mission_id) REFERENCES missions(mission_id) ON DELETE CASCADE
                )
                """
            )
            row = self._connection.execute(
                "SELECT value FROM argus_budget_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO argus_budget_metadata(key, value) VALUES ('schema_version', ?)",
                    (str(_BUDGET_SCHEMA_VERSION),),
                )
            else:
                try:
                    version = int(row[0])
                except (TypeError, ValueError) as exc:
                    raise BudgetError("budget schema version is invalid") from exc
                if version != _BUDGET_SCHEMA_VERSION:
                    raise BudgetError(f"unsupported budget schema version: {version}")
        self._validate_rows()

    def _validate_rows(self) -> None:
        policies = self._connection.execute("SELECT * FROM budget_policies").fetchall()
        for row in policies:
            policy = self._policy_from_row(row)
            if policy.policy_hash != row["policy_hash"]:
                raise BudgetError(
                    f"persisted budget policy hash mismatch for {row['mission_id']}"
                )
        reservations = self._connection.execute("SELECT * FROM spend_reservations").fetchall()
        for row in reservations:
            reservation = self._reservation_from_row(row)
            if reservation.policy_version < 1 or not reservation.policy_hash:
                raise BudgetError("invalid persisted reservation policy provenance")
            if (
                reservation.state is ReservationState.COMMITTED
                and reservation.committed_usd is None
            ):
                raise BudgetError("committed reservation is missing committed spend")
            if (
                reservation.state is not ReservationState.COMMITTED
                and reservation.committed_usd is not None
            ):
                raise BudgetError("non-committed reservation has committed spend")

    def _policy_from_row(self, row: sqlite3.Row) -> BudgetPolicy:
        try:
            policy = BudgetPolicy(
                mission_id=row["mission_id"],
                policy_version=int(row["policy_version"]),
                worker_attempt_limit=(
                    int(row["worker_attempt_limit"])
                    if row["worker_attempt_limit"] is not None
                    else None
                ),
                effect_attempt_limit=(
                    int(row["effect_attempt_limit"])
                    if row["effect_attempt_limit"] is not None
                    else None
                ),
                spend_limit_usd=(
                    _money(row["spend_limit_usd"], allow_zero=True)
                    if row["spend_limit_usd"] is not None
                    else None
                ),
            )
        except (TypeError, ValueError, InvalidOperation) as exc:
            raise BudgetError("invalid persisted budget policy") from exc
        if policy.policy_hash != row["policy_hash"]:
            raise BudgetError(
                f"persisted budget policy hash mismatch for {row['mission_id']}"
            )
        return policy

    def _reservation_from_row(self, row: sqlite3.Row) -> SpendReservation:
        try:
            state = ReservationState(row["state"])
            reserved = _money(row["reserved_usd"], allow_zero=True)
            committed = (
                _money(row["committed_usd"], allow_zero=True)
                if row["committed_usd"] is not None
                else None
            )
        except (ValueError, InvalidOperation) as exc:
            raise BudgetError("invalid persisted spend reservation") from exc
        if state is ReservationState.COMMITTED and committed is None:
            raise BudgetError("committed reservation is missing committed spend")
        if state is not ReservationState.COMMITTED and committed is not None:
            raise BudgetError("non-committed reservation has committed spend")
        if committed is not None and committed > reserved:
            raise BudgetError("committed spend exceeds durable reservation")
        return SpendReservation(
            reservation_key=row["reservation_key"],
            mission_id=row["mission_id"],
            state=state,
            reserved_usd=reserved,
            committed_usd=committed,
            policy_version=int(row["policy_version"]),
            policy_hash=row["policy_hash"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _append_event(
        self,
        *,
        mission_id: str,
        event_type: str,
        reservation_key: str | None,
        amount_usd: Decimal | None,
        reason: str | None,
        policy: BudgetPolicy,
        recorded_at: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO budget_events(
                mission_id, event_type, reservation_key, amount_usd, reason,
                policy_version, policy_hash, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                mission_id,
                event_type,
                reservation_key,
                _money_text(amount_usd) if amount_usd is not None else None,
                reason,
                policy.policy_version,
                policy.policy_hash,
                recorded_at,
            ),
        )


def _money(value: Decimal | str | int | float, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("money amount must be numeric")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("money amount must be a finite decimal") from exc
    if not amount.is_finite():
        raise ValueError("money amount must be finite")
    if amount < 0 or (amount == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"money amount must be {qualifier}")
    return amount


def _money_text(value: Decimal | None) -> str:
    if value is None:
        return "none"
    normalized = value.normalize()
    text = format(normalized, "f")
    return "0" if text in {"-0", "0E+0"} else text


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
