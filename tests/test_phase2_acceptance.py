from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from argus.attempts import (
    AmbiguousAttemptError,
    AttemptDecisionKind,
    AttemptPolicy,
    AttemptStore,
)
from argus.bounded_process import BoundedProcessConfig
from argus.cli import main as cli_main
from argus.manifest import load_manifest
from argus.model import MissionState, StepEnvelope, StepState
from argus.phase2_runtime import Phase2Runtime
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore
from argus.worker_adapter import JsonSubprocessWorker


_DUE = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _create_single_step(path: Path, *, mission_id: str = "mission-1") -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            mission_id,
            [
                StepEnvelope(
                    mission_id=mission_id,
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.remote",
                    payload={
                        "cycle_id": "cycle-001",
                        "input_digest": "sha256:opaque-input",
                        "policy_version": "consumer-policy-v1",
                        "artifact_refs": ["consumer://input/a"],
                    },
                )
            ],
        )
    with DurableScheduler(path) as scheduler:
        scheduler.schedule(mission_id, "step-0", _DUE)


def _worker_script(tmp_path: Path, body: str, *, name: str) -> Path:
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return script


def _json_worker(script: Path, *args: str, timeout: float = 2.0) -> JsonSubprocessWorker:
    return JsonSubprocessWorker(
        BoundedProcessConfig(
            command=(sys.executable, str(script), *args),
            timeout_seconds=timeout,
            termination_grace_seconds=0.25,
            max_stdout_bytes=64_000,
            max_stderr_bytes=16_000,
        )
    )


def test_due_at_survives_real_process_restart_and_never_wakes_early(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_single_step(path)

    probe = (
        "import json,sys; "
        "from argus.scheduler import DurableScheduler; "
        "p=sys.argv[1]; at=sys.argv[2]; "
        "s=DurableScheduler(p); "
        "print(json.dumps([x.step_id for x in s.due(at)])); s.close()"
    )

    before = subprocess.run(
        [sys.executable, "-c", probe, str(path), "2030-01-02T11:59:59Z"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(before.stdout) == []

    # A completely new interpreter reopens the same SQLite state at the due boundary.
    due = subprocess.run(
        [sys.executable, "-c", probe, str(path), "2030-01-02T12:00:00Z"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(due.stdout) == ["step-0"]


def test_reference_workload_polls_due_steps_through_real_subprocess_and_completes(
    tmp_path, capsys
) -> None:
    path = tmp_path / "argus.db"
    manifest_path = Path(__file__).parents[1] / "examples" / "reference-workload-v1.json"
    manifest = load_manifest(manifest_path)

    with SqliteMissionStore(path) as store:
        store.create_mission(manifest.mission_id, manifest.steps)
    with DurableScheduler(path) as scheduler:
        for step in manifest.steps:
            scheduler.schedule(manifest.mission_id, step.step_id, _DUE)

    script = _worker_script(
        tmp_path,
        """import json, sys
request = json.load(sys.stdin)
print(json.dumps({
    'schema_version': 1,
    'request_id': request['request_id'],
    'outcome': 'success',
    'output': {'artifact_ref': 'consumer://result/' + request['request_id']},
    'message': None,
    'retry_after_seconds': None,
    'usage': {'input_tokens': 11, 'output_tokens': 7, 'cost_usd': 0.002},
}))
""",
        name="success_worker.py",
    )
    worker = _json_worker(script)
    runtime = Phase2Runtime(path)

    assert runtime.run_next_due({"fixture.echo": worker}, now=_DUE - timedelta(seconds=1)) is None

    # Duplicate read-only polling is harmless: it creates no attempt by itself.
    assert [item.step_id for item in runtime.due(_DUE)] == ["collect-evidence"]
    assert [item.step_id for item in runtime.due(_DUE)] == ["collect-evidence"]
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts(manifest.mission_id, "collect-evidence") == []

    for expected in ("collect-evidence", "produce-decision", "prepare-effect"):
        decision = runtime.run_next_due({"fixture.echo": worker}, now=_DUE)
        assert decision is not None
        assert decision.kind is AttemptDecisionKind.SUCCEEDED
        assert decision.attempt.step_id == expected

    assert runtime.run_next_due({"fixture.echo": worker}, now=_DUE) is None
    with SqliteMissionStore(path) as store:
        assert store.load_mission(manifest.mission_id).state is MissionState.COMPLETED
        assert all(step.state is StepState.SUCCEEDED for step in store.list_steps(manifest.mission_id))

    assert cli_main(["inspect", "--store", str(path), manifest.mission_id]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["state"] == "completed"
    assert len(inspected["attempts"]) == 3
    assert [attempt["outcome"] for attempt in inspected["attempts"]] == [
        "success",
        "success",
        "success",
    ]
    assert all(attempt["input_tokens"] == 11 for attempt in inspected["attempts"])
    assert all(attempt["output_tokens"] == 7 for attempt in inspected["attempts"])
    assert all(attempt["cost_usd"] == pytest.approx(0.002) for attempt in inspected["attempts"])
    assert all("payload" not in attempt for attempt in inspected["attempts"])

    forbidden = ("youtube", "shorts", "retention", "editorial", "hook")
    fixture_text = manifest_path.read_text(encoding="utf-8").lower()
    assert not any(term in fixture_text for term in forbidden)


def test_transient_failure_retries_at_injected_clock_boundary_then_succeeds(tmp_path) -> None:
    path = tmp_path / "argus.db"
    counter = tmp_path / "counter.txt"
    _create_single_step(path)

    script = _worker_script(
        tmp_path,
        """import json, pathlib, sys
request = json.load(sys.stdin)
path = pathlib.Path(sys.argv[1])
count = int(path.read_text()) if path.exists() else 0
path.write_text(str(count + 1))
if count == 0:
    outcome = 'retryable_failure'
    output = {'reason': 'transient'}
    retry = 5
    usage = None
else:
    outcome = 'success'
    output = {'artifact_ref': 'consumer://result/retried'}
    retry = None
    usage = {'input_tokens': 21, 'output_tokens': 8, 'cost_usd': 0.003}
print(json.dumps({
    'schema_version': 1,
    'request_id': request['request_id'],
    'outcome': outcome,
    'output': output,
    'message': None,
    'retry_after_seconds': retry,
    'usage': usage,
}))
""",
        name="retry_then_success.py",
    )
    worker = _json_worker(script, str(counter))
    runtime = Phase2Runtime(path)
    policy = AttemptPolicy(max_attempts=3, retry_delay_seconds=60)

    first = runtime.execute_due_step("mission-1", "step-0", worker, policy=policy, now=_DUE)
    assert first.kind is AttemptDecisionKind.RETRY_SCHEDULED
    assert first.retry_due_at == "2030-01-02T12:00:05.000000Z"
    assert counter.read_text() == "1"

    assert runtime.due(_DUE + timedelta(seconds=4, microseconds=999_999)) == []

    second = runtime.execute_due_step(
        "mission-1",
        "step-0",
        worker,
        policy=policy,
        now=_DUE + timedelta(seconds=5),
    )
    assert second.kind is AttemptDecisionKind.SUCCEEDED
    assert counter.read_text() == "2"

    with AttemptStore(path) as attempts:
        history = attempts.list_attempts("mission-1", "step-0")
        assert [attempt.attempt_no for attempt in history] == [1, 2]
        assert history[0].outcome is not None and history[0].outcome.value == "retryable_failure"
        assert history[1].outcome is not None and history[1].outcome.value == "success"
        assert history[1].input_tokens == 21
        assert history[1].output_tokens == 8
        assert history[1].cost_usd == pytest.approx(0.003)

    with SqliteMissionStore(path) as store:
        assert store.load_mission("mission-1").state is MissionState.COMPLETED


def test_malformed_and_permanent_worker_results_fail_deterministically(tmp_path) -> None:
    cases = {
        "malformed": "print('```json not-the-protocol ```')",
        "permanent": """import json, sys
request=json.load(sys.stdin)
print(json.dumps({
  'schema_version':1,
  'request_id':request['request_id'],
  'outcome':'permanent_failure',
  'output':{'reason':'invalid input'},
  'message':'cannot complete',
  'retry_after_seconds':None,
  'usage':None,
}))
""",
    }

    for name, body in cases.items():
        path = tmp_path / f"{name}.db"
        _create_single_step(path)
        worker = _json_worker(_worker_script(tmp_path, body, name=f"{name}_worker.py"))
        decision = Phase2Runtime(path).execute_due_step(
            "mission-1", "step-0", worker, now=_DUE
        )
        assert decision.kind is AttemptDecisionKind.FAILED
        with SqliteMissionStore(path) as store:
            assert store.load_mission("mission-1").state is MissionState.FAILED
            assert store.load_step("mission-1", "step-0").state is StepState.FAILED
        with DurableScheduler(path) as scheduler:
            assert scheduler.get("mission-1", "step-0") is None


@pytest.mark.skipif(os.name != "posix", reason="Phase 2 process-tree proof targets POSIX process groups")
def test_real_process_timeout_retries_then_exhausts_attempt_limit(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_single_step(path)
    script = _worker_script(
        tmp_path,
        """import json, sys, time
json.load(sys.stdin)
time.sleep(5)
""",
        name="timeout_worker.py",
    )
    worker = _json_worker(script, timeout=0.05)
    runtime = Phase2Runtime(path)
    policy = AttemptPolicy(max_attempts=2, retry_delay_seconds=1, retry_timeouts=True)

    first = runtime.execute_due_step("mission-1", "step-0", worker, policy=policy, now=_DUE)
    assert first.kind is AttemptDecisionKind.RETRY_SCHEDULED
    assert first.attempt.outcome is not None and first.attempt.outcome.value == "timeout"
    assert first.retry_due_at == "2030-01-02T12:00:01.000000Z"

    second = runtime.execute_due_step(
        "mission-1",
        "step-0",
        worker,
        policy=policy,
        now=_DUE + timedelta(seconds=1),
    )
    assert second.kind is AttemptDecisionKind.FAILED
    assert second.attempt.outcome is not None and second.attempt.outcome.value == "timeout"

    with AttemptStore(path) as attempts:
        assert len(attempts.list_attempts("mission-1", "step-0")) == 2
    with SqliteMissionStore(path) as store:
        step = store.load_step("mission-1", "step-0")
        assert step.state is StepState.FAILED
        assert step.result is not None
        assert step.result.output["error"] == "attempt_limit_exhausted:timeout"
        assert store.load_mission("mission-1").state is MissionState.FAILED


def test_real_process_death_after_attempt_start_blocks_blind_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_single_step(path)

    crash = (
        "import os,sys; "
        "from argus.attempts import AttemptStore; "
        "s=AttemptStore(sys.argv[1]); "
        "s.begin_attempt('mission-1','step-0',started_at=sys.argv[2]); "
        "s.close(); os._exit(91)"
    )
    child = subprocess.run(
        [sys.executable, "-c", crash, str(path), "2030-01-02T12:00:00Z"],
        check=False,
    )
    assert child.returncode == 91

    marker = tmp_path / "worker-called"
    script = _worker_script(
        tmp_path,
        """import pathlib, sys
pathlib.Path(sys.argv[1]).write_text('called')
raise SystemExit(1)
""",
        name="must_not_run.py",
    )
    worker = _json_worker(script, str(marker))

    with pytest.raises(AmbiguousAttemptError, match="no durable outcome"):
        Phase2Runtime(path).execute_due_step(
            "mission-1", "step-0", worker, now=_DUE + timedelta(seconds=1)
        )
    assert not marker.exists()


def test_reconcile_closes_safe_crash_boundary_after_final_step_commit(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _create_single_step(path)

    # Simulate the safe narrow boundary: the logical step is already committed as
    # succeeded but the process dies before mission completion is persisted.
    with SqliteMissionStore(path) as store:
        store.transition_mission("mission-1", MissionState.RUNNING)
        store.transition_step("mission-1", "step-0", StepState.RUNNING)
        from argus.model import StepResult, StepResultKind

        store.transition_step(
            "mission-1",
            "step-0",
            StepState.SUCCEEDED,
            result=StepResult(kind=StepResultKind.SUCCESS, output={"ok": True}),
        )
        assert store.load_mission("mission-1").state is MissionState.RUNNING

    assert Phase2Runtime(path).reconcile_mission("mission-1") is MissionState.COMPLETED
    with SqliteMissionStore(path) as store:
        assert store.load_mission("mission-1").state is MissionState.COMPLETED
