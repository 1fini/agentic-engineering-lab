from __future__ import annotations

from datetime import datetime, timezone
import json

from argus.attempts import AttemptPolicy, AttemptStore
from argus.cli import main
from argus.model import StepEnvelope
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerOutcome, WorkerResponse, WorkerUsage


def test_status_and_inspect_expose_attempt_metadata_without_worker_payload(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    due = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.work",
                    payload={"secret_payload": "do-not-print"},
                )
            ],
        )
    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", due)
    with AttemptStore(path) as store:
        attempt = store.begin_attempt("mission-1", "step-0", started_at=due)
        store.complete_attempt(
            attempt,
            WorkerInvocation(
                outcome=InvocationOutcome.RETRYABLE_FAILURE,
                duration_ms=123,
                exit_code=0,
                response=WorkerResponse(
                    request_id=attempt.request_id,
                    outcome=WorkerOutcome.RETRYABLE_FAILURE,
                    output={"private_output": "not-for-inspect"},
                    retry_after_seconds=10,
                    usage=WorkerUsage(input_tokens=20, output_tokens=5, cost_usd=0.002),
                ),
            ),
            completed_at=due,
            policy=AttemptPolicy(max_attempts=3),
        )

    assert main(["status", "--store", str(path), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["steps"][0]["attempt_count"] == 1
    assert status["steps"][0]["latest_attempt_outcome"] == "retryable_failure"

    assert main(["inspect", "--store", str(path), "mission-1"]) == 0
    inspected_text = capsys.readouterr().out
    inspected = json.loads(inspected_text)
    assert inspected["attempts"][0]["duration_ms"] == 123
    assert inspected["attempts"][0]["input_tokens"] == 20
    assert inspected["attempts"][0]["cost_usd"] == 0.002
    assert "do-not-print" not in inspected_text
    assert "not-for-inspect" not in inspected_text
