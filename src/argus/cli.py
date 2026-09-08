"""ARGUS Phase 1 command-line control surface."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from argus import __version__
from argus.fixture_workers import build_fixture_registry
from argus.manifest import ManifestError, load_manifest
from argus.model import ArgusStateError, MissionState
from argus.runner import BlockedMissionError, MissionRunner, WorkerRegistry
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
