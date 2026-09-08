from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import pytest

from argus.bounded_process import BoundedProcess, BoundedProcessConfig, ProcessOutcome
from argus.worker_adapter import InvocationOutcome, JsonSubprocessWorker
from argus.worker_protocol import WorkerRequest


def test_json_subprocess_worker_success(tmp_path) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        """
import json, sys
request = json.load(sys.stdin)
print(json.dumps({
    "schema_version": 1,
    "request_id": request["request_id"],
    "outcome": "success",
    "output": {"operation": request["operation"]},
    "message": None,
    "retry_after_seconds": None,
    "usage": None,
}))
""".strip(),
        encoding="utf-8",
    )
    request = WorkerRequest(request_id="req-1", operation="fixture.work", payload={"x": 1})
    worker = JsonSubprocessWorker(
        BoundedProcessConfig(command=(sys.executable, str(script)), timeout_seconds=2)
    )

    result = worker.invoke(request)
    assert result.outcome is InvocationOutcome.SUCCESS
    assert result.response is not None
    assert result.response.output == {"operation": "fixture.work"}


def test_process_error_and_malformed_output_are_distinct(tmp_path) -> None:
    fail = BoundedProcess(
        BoundedProcessConfig(
            command=(sys.executable, "-c", "import sys; print('nope', file=sys.stderr); sys.exit(7)"),
            timeout_seconds=2,
        )
    ).run()
    assert fail.outcome is ProcessOutcome.PROCESS_ERROR
    assert fail.exit_code == 7
    assert fail.stderr_excerpt == "nope\n"

    request = WorkerRequest(request_id="req-1", operation="fixture.work", payload={})
    malformed = JsonSubprocessWorker(
        BoundedProcessConfig(
            command=(sys.executable, "-c", "print('not-json')"),
            timeout_seconds=2,
        )
    ).invoke(request)
    assert malformed.outcome is InvocationOutcome.MALFORMED_OUTPUT
    assert "valid JSON" in (malformed.detail or "")


def test_output_limit_is_enforced_without_retaining_unbounded_output() -> None:
    result = BoundedProcess(
        BoundedProcessConfig(
            command=(sys.executable, "-c", "print('x' * 10000)"),
            timeout_seconds=2,
            max_stdout_bytes=128,
        )
    ).run()
    assert result.outcome is ProcessOutcome.OUTPUT_LIMIT
    assert len(result.stdout) == 128
    assert "exceeded" in (result.detail or "")


@pytest.mark.skipif(os.name != "posix", reason="process-group proof is POSIX-specific in Phase 2")
def test_timeout_terminates_process_group(tmp_path) -> None:
    marker = tmp_path / "heartbeat.txt"
    child = tmp_path / "child.py"
    parent = tmp_path / "parent.py"
    child.write_text(
        """
import pathlib, sys, time
path = pathlib.Path(sys.argv[1])
while True:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("beat\\n")
        handle.flush()
    time.sleep(0.03)
""".strip(),
        encoding="utf-8",
    )
    parent.write_text(
        """
import subprocess, sys, time
subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])
time.sleep(60)
""".strip(),
        encoding="utf-8",
    )

    result = BoundedProcess(
        BoundedProcessConfig(
            command=(sys.executable, str(parent), str(child), str(marker)),
            timeout_seconds=0.25,
            termination_grace_seconds=0.5,
        )
    ).run()
    assert result.outcome is ProcessOutcome.TIMEOUT

    time.sleep(0.15)
    first = marker.read_text(encoding="utf-8") if marker.exists() else ""
    time.sleep(0.15)
    second = marker.read_text(encoding="utf-8") if marker.exists() else ""
    assert first
    assert second == first
