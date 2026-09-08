"""ARGUS command-line control surface."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from argus import __version__
from argus.attempts import AttemptStore
from argus.effect_attempts import EffectAttemptStore
from argus.effects import EffectStore
from argus.fixture_workers import build_fixture_registry
from argus.manifest import ManifestError, load_manifest
from argus.model import ArgusStateError, MissionState
from argus.runner import BlockedMissionError, MissionRunner, WorkerRegistry
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore

_EXIT_INPUT = 2
_EXIT_STATE = 3
_EXIT_BLOCKED = 4
_EXIT_MISSION_FAILED = 5


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="argus",
        description="Durable control-plane runtime for long-running AI agent missions",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="create/resume and run a mission")
    run_parser.add_argument("--store", required=True, help="path to ARGUS SQLite state")
    run_parser.add_argument("--manifest", required=True, help="versioned mission JSON file")
    run_parser.add_argument(
        "--fixture-workers",
        action="store_true",
        help="enable only the explicit Phase 1 fixture worker registry",
    )

    status_parser = subparsers.add_parser("status", help="show durable mission status")
    status_parser.add_argument("--store", required=True, help="path to ARGUS SQLite state")
    status_parser.add_argument("mission_id")

    inspect_parser = subparsers.add_parser("inspect", help="inspect durable mission history")
    inspect_parser.add_argument("--store", required=True, help="path to ARGUS SQLite state")
    inspect_parser.add_argument("mission_id")

    schedule_parser = subparsers.add_parser(
        "schedule", help="persist future eligibility for a pending step"
    )
    schedule_parser.add_argument("--store", required=True, help="path to ARGUS SQLite state")
    schedule_parser.add_argument("--due-at", required=True, help="timezone-aware ISO-8601 timestamp")
    schedule_parser.add_argument("mission_id")
    schedule_parser.add_argument("step_id")

    due_parser = subparsers.add_parser("due", help="list scheduled steps eligible at a time")
    due_parser.add_argument("--store", required=True, help="path to ARGUS SQLite state")
    due_parser.add_argument(
        "--at",
        help="timezone-aware ISO-8601 timestamp; defaults to current UTC time",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "run":
            return _run(args)
        if args.command == "status":
            return _status(args)
        if args.command == "inspect":
            return _inspect(args)
        if args.command == "schedule":
            return _schedule(args)
        if args.command == "due":
            return _due(args)
        raise AssertionError(f"unhandled command: {args.command}")
    except ManifestError as exc:
        return _emit_error(_EXIT_INPUT, "input_error", exc)
    except BlockedMissionError as exc:
        return _emit_error(_EXIT_BLOCKED, "blocked", exc)
    except (ArgusStateError, KeyError) as exc:
        return _emit_error(_EXIT_STATE, "state_error", exc)


def _run(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    workers = build_fixture_registry() if args.fixture_workers else WorkerRegistry()
    with SqliteMissionStore(args.store) as store:
        store.create_mission(manifest.mission_id, manifest.steps)
        summary = MissionRunner(store, workers).run(manifest.mission_id)
    _emit({"schema_version": 1, "command": "run", **summary.as_dict()})
    if summary.state is MissionState.FAILED:
        return _EXIT_MISSION_FAILED
    return 0


def _status(args: argparse.Namespace) -> int:
    with SqliteMissionStore(args.store) as store:
        mission = store.load_mission(args.mission_id)
        steps = store.list_steps(args.mission_id)
    with DurableScheduler(args.store) as scheduler:
        schedules = {
            item.step_id: item.due_at for item in scheduler.list_for_mission(args.mission_id)
        }
    with AttemptStore(args.store) as attempts:
        attempt_summary = {
            step.envelope.step_id: attempts.list_attempts(
                args.mission_id, step.envelope.step_id
            )
            for step in steps
        }
    with EffectStore(args.store) as effect_store:
        effects = effect_store.list_for_mission(args.mission_id)
    with EffectAttemptStore(args.store) as effect_attempt_store:
        effect_attempt_summary = {
            (effect.intent.step_id, effect.intent.effect_id): effect_attempt_store.list_attempts(
                args.mission_id,
                effect.intent.step_id,
                effect.intent.effect_id,
            )
            for effect in effects
        }
    effect_summary = {
        step.envelope.step_id: [
            effect for effect in effects if effect.intent.step_id == step.envelope.step_id
        ]
        for step in steps
    }
    _emit(
        {
            "schema_version": 1,
            "command": "status",
            "mission_id": mission.mission_id,
            "state": mission.state.value,
            "steps": [
                {
                    "step_id": step.envelope.step_id,
                    "ordinal": step.envelope.ordinal,
                    "operation": step.envelope.operation,
                    "state": step.state.value,
                    "due_at": schedules.get(step.envelope.step_id),
                    "attempt_count": len(attempt_summary[step.envelope.step_id]),
                    "latest_attempt_state": (
                        attempt_summary[step.envelope.step_id][-1].state.value
                        if attempt_summary[step.envelope.step_id]
                        else None
                    ),
                    "latest_attempt_outcome": (
                        attempt_summary[step.envelope.step_id][-1].outcome.value
                        if attempt_summary[step.envelope.step_id]
                        and attempt_summary[step.envelope.step_id][-1].outcome is not None
                        else None
                    ),
                    "effect_count": len(effect_summary[step.envelope.step_id]),
                    "effect_states": [
                        effect.state.value
                        for effect in effect_summary[step.envelope.step_id]
                    ],
                    "effect_attempt_count": sum(
                        len(
                            effect_attempt_summary.get(
                                (effect.intent.step_id, effect.intent.effect_id), []
                            )
                        )
                        for effect in effect_summary[step.envelope.step_id]
                    ),
                }
                for step in steps
            ],
        }
    )
    return 0


def _inspect(args: argparse.Namespace) -> int:
    with SqliteMissionStore(args.store) as store:
        mission = store.load_mission(args.mission_id)
        steps = store.list_steps(args.mission_id)
        journal = store.journal(args.mission_id)
    with DurableScheduler(args.store) as scheduler:
        schedules = {
            item.step_id: item.due_at for item in scheduler.list_for_mission(args.mission_id)
        }
        schedule_history = scheduler.history(args.mission_id)
    with AttemptStore(args.store) as attempt_store:
        attempts = [
            attempt
            for step in steps
            for attempt in attempt_store.list_attempts(
                args.mission_id, step.envelope.step_id
            )
        ]
    with EffectStore(args.store) as effect_store:
        effects = effect_store.list_for_mission(args.mission_id)
        effect_history = effect_store.history(args.mission_id)
    with EffectAttemptStore(args.store) as effect_attempt_store:
        effect_attempts = [
            attempt
            for effect in effects
            for attempt in effect_attempt_store.list_attempts(
                args.mission_id,
                effect.intent.step_id,
                effect.intent.effect_id,
            )
        ]
    _emit(
        {
            "schema_version": 1,
            "command": "inspect",
            "mission_id": mission.mission_id,
            "state": mission.state.value,
            "steps": [
                {
                    "step_id": step.envelope.step_id,
                    "ordinal": step.envelope.ordinal,
                    "operation": step.envelope.operation,
                    "payload_version": step.envelope.payload_version,
                    "idempotency_key": step.envelope.idempotency_key,
                    "state": step.state.value,
                    "result_kind": step.result.kind.value if step.result else None,
                    "due_at": schedules.get(step.envelope.step_id),
                }
                for step in steps
            ],
            "journal": [
                {
                    "sequence": entry.sequence,
                    "step_id": entry.step_id,
                    "entity_type": entry.entity_type,
                    "event_type": entry.event_type,
                    "from_state": entry.from_state,
                    "to_state": entry.to_state,
                    "recorded_at": entry.recorded_at,
                }
                for entry in journal
            ],
            "schedule_history": [
                {
                    "sequence": entry.sequence,
                    "step_id": entry.step_id,
                    "event_type": entry.event_type,
                    "previous_due_at": entry.previous_due_at,
                    "due_at": entry.due_at,
                    "recorded_at": entry.recorded_at,
                }
                for entry in schedule_history
            ],
            "attempts": [
                {
                    "step_id": attempt.step_id,
                    "attempt_no": attempt.attempt_no,
                    "request_id": attempt.request_id,
                    "state": attempt.state.value,
                    "started_at": attempt.started_at,
                    "completed_at": attempt.completed_at,
                    "outcome": attempt.outcome.value if attempt.outcome else None,
                    "duration_ms": attempt.duration_ms,
                    "exit_code": attempt.exit_code,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "cost_usd": attempt.cost_usd,
                    "retry_due_at": attempt.retry_due_at,
                }
                for attempt in attempts
            ],
            "effects": [
                {
                    "step_id": effect.intent.step_id,
                    "effect_id": effect.intent.effect_id,
                    "operation": effect.intent.operation,
                    "payload_version": effect.intent.payload_version,
                    "correlation_key": effect.intent.correlation_key,
                    "state": effect.state.value,
                    "receipt_outcome": (
                        effect.receipt.outcome.value if effect.receipt else None
                    ),
                    "receipt_source": (
                        effect.receipt.source.value if effect.receipt else None
                    ),
                    "created_at": effect.created_at,
                    "updated_at": effect.updated_at,
                }
                for effect in effects
            ],
            "effect_attempts": [
                {
                    "step_id": attempt.step_id,
                    "effect_id": attempt.effect_id,
                    "attempt_no": attempt.attempt_no,
                    "attempt_key": attempt.attempt_key,
                    "state": attempt.state.value,
                    "started_at": attempt.started_at,
                    "resolved_at": attempt.resolved_at,
                    "receipt_outcome": (
                        attempt.receipt_outcome.value
                        if attempt.receipt_outcome is not None
                        else None
                    ),
                    "receipt_source": (
                        attempt.receipt_source.value
                        if attempt.receipt_source is not None
                        else None
                    ),
                }
                for attempt in effect_attempts
            ],
            "effect_history": [
                {
                    "sequence": event.sequence,
                    "step_id": event.step_id,
                    "effect_id": event.effect_id,
                    "event_type": event.event_type,
                    "from_state": event.from_state,
                    "to_state": event.to_state,
                    "recorded_at": event.recorded_at,
                }
                for event in effect_history
            ],
        }
    )
    return 0


def _schedule(args: argparse.Namespace) -> int:
    with DurableScheduler(args.store) as scheduler:
        entry = scheduler.schedule(args.mission_id, args.step_id, args.due_at)
    _emit(
        {
            "schema_version": 1,
            "command": "schedule",
            "mission_id": entry.mission_id,
            "step_id": entry.step_id,
            "due_at": entry.due_at,
        }
    )
    return 0


def _due(args: argparse.Namespace) -> int:
    with DurableScheduler(args.store) as scheduler:
        entries = scheduler.due(args.at)
    _emit(
        {
            "schema_version": 1,
            "command": "due",
            "steps": [
                {
                    "mission_id": entry.mission_id,
                    "step_id": entry.step_id,
                    "due_at": entry.due_at,
                }
                for entry in entries
            ],
        }
    )
    return 0


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def _emit_error(code: int, kind: str, exc: BaseException) -> int:
    payload = {
        "schema_version": 1,
        "error": kind,
        "message": str(exc),
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")), file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
