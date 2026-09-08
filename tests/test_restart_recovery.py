from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from argus.cli import main
from argus.fixture_workers import build_fixture_registry
from argus.manifest import load_manifest
from argus.model import MissionState, StepEnvelope, StepState
from argus.runner import BlockedMissionError, MissionRunner, WorkerRegistry
from argus.store import SqliteMissionStore


_CRASH_AFTER_COMMIT = textwrap.dedent(
    r"""
    import os
    import sys

    from argus.model import StepEnvelope, StepState
    from argus.runner import MissionRunner, WorkerRegistry
    from argus.store import SqliteMissionStore

    db_path, execution_log = sys.argv[1], sys.argv[2]

    steps = [
        StepEnvelope(
            mission_id="mission-1",
            step_id=f"step-{index}",
            ordinal=index,
            operation="fixture.work",
            payload={"index": index},
        )
        for index in range(3)
    ]

    class CrashAfterCommitStore(SqliteMissionStore):
        def transition_step(self, mission_id, step_id, target, *, result=None):
            record = super().transition_step(
                mission_id, step_id, target, result=result
            )
            if step_id == "step-0" and target is StepState.SUCCEEDED:
                os._exit(86)
            return record

    def worker(step):
        with open(execution_log, "a", encoding="utf-8") as handle:
            handle.write(step.step_id + "\n")
        return {"handled": step.step_id}

    workers = WorkerRegistry()
    workers.register("fixture.work", worker)
    with CrashAfterCommitStore(db_path) as store:
        store.create_mission("mission-1", steps)
        MissionRunner(store, workers).run("mission-1")
    """
)


_CRASH_INSIDE_WORKER = textwrap.dedent(
    r"""
    import os
    import sys

    from argus.model import StepEnvelope
    from argus.runner import MissionRunner, WorkerRegistry
    from argus.store import SqliteMissionStore

    db_path, execution_log = sys.argv[1], sys.argv[2]
    steps = [
        StepEnvelope(
            mission_id="mission-1",
            step_id=f"step-{index}",
            ordinal=index,
            operation="fixture.work",
            payload={"index": index},
        )
        for index in range(3)
    ]

    def worker(step):
        with open(execution_log, "a", encoding="utf-8") as handle:
            handle.write(step.step_id + "\n")
        os._exit(87)

    workers = WorkerRegistry()
    workers.register("fixture.work", worker)
    with SqliteMissionStore(db_path) as store:
        store.create_mission("mission-1", steps)
        MissionRunner(store, workers).run("mission-1")
    """
)


def _steps() -> list[StepEnvelope]:
    return [
        StepEnvelope(
            mission_id="mission-1",
            step_id=f"step-{index}",
            ordinal=index,
            operation="fixture.work",
            payload={"index": index},
        )
        for index in range(3)
    ]


def _append_worker(log_path: Path):
    def worker(step: StepEnvelope):
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(step.step_id + "\n")
        return {"handled": step.step_id}

    return worker


def _run_child(script: str, db_path: Path, execution_log: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script, str(db_path), str(execution_log)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_real_process_restart_skips_step_committed_before_crash(tmp_path, capsys) -> None:
    db_path = tmp_path / "argus.db"
    execution_log = tmp_path / "executions.log"

    crashed = _run_child(_CRASH_AFTER_COMMIT, db_path, execution_log)
    assert crashed.returncode == 86
    assert execution_log.read_text(encoding="utf-8").splitlines() == ["step-0"]

    with SqliteMissionStore(db_path) as store:
        assert store.load_mission("mission-1").state is MissionState.RUNNING
        assert store.load_step("mission-1", "step-0").state is StepState.SUCCEEDED
        persisted_key = store.load_step("mission-1", "step-0").envelope.idempotency_key

        workers = WorkerRegistry()
        workers.register("fixture.work", _append_worker(execution_log))
        summary = MissionRunner(store, workers).run("mission-1")
        assert summary.state is MissionState.COMPLETED
        assert summary.executed_steps == ("step-1", "step-2")
        assert store.load_step("mission-1", "step-0").envelope.idempotency_key == persisted_key

        step_zero_transitions = [
            (entry.from_state, entry.to_state)
            for entry in store.journal("mission-1")
            if entry.step_id == "step-0" and entry.event_type == "state_transition"
        ]
        assert step_zero_transitions == [
            ("pending", "running"),
            ("running", "succeeded"),
        ]

        repeated = MissionRunner(store, workers).run("mission-1")
        assert repeated.executed_steps == ()

    assert execution_log.read_text(encoding="utf-8").splitlines() == [
        "step-0",
        "step-1",
        "step-2",
    ]

    assert main(["inspect", "--store", str(db_path), "mission-1"]) == 0
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["state"] == "completed"
    assert evidence["steps"][0]["state"] == "succeeded"
    assert evidence["journal"][-1]["to_state"] == "completed"
    json.dumps(evidence)


def test_process_death_inside_running_step_blocks_instead_of_blind_retry(tmp_path) -> None:
    db_path = tmp_path / "argus.db"
    execution_log = tmp_path / "executions.log"

    crashed = _run_child(_CRASH_INSIDE_WORKER, db_path, execution_log)
    assert crashed.returncode == 87
    assert execution_log.read_text(encoding="utf-8").splitlines() == ["step-0"]

    with SqliteMissionStore(db_path) as store:
        assert store.load_step("mission-1", "step-0").state is StepState.RUNNING
        workers = WorkerRegistry()
        workers.register("fixture.work", _append_worker(execution_log))
        with pytest.raises(BlockedMissionError):
            MissionRunner(store, workers).run("mission-1")

    assert execution_log.read_text(encoding="utf-8").splitlines() == ["step-0"]


def test_reference_workload_fixture_is_generic_and_executable(tmp_path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    fixture_path = repository_root / "examples" / "reference-workload-v1.json"
    raw = fixture_path.read_text(encoding="utf-8")
    lowered = raw.lower()
    for forbidden in ("youtube", "short", "retention", "editorial", "hook"):
        assert forbidden not in lowered

    manifest = load_manifest(fixture_path)
    assert len(manifest.steps) == 3
    assert manifest.steps[0].payload["policy_version"] == "consumer-policy-v1"

    with SqliteMissionStore(tmp_path / "reference.db") as store:
        store.create_mission(manifest.mission_id, manifest.steps)
        summary = MissionRunner(store, build_fixture_registry()).run(manifest.mission_id)
        assert summary.state is MissionState.COMPLETED
