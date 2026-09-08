"""OpenCode CLI adapter for the generic ARGUS bounded worker protocol.

OpenCode is an execution backend only. ARGUS remains the orchestration authority.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import tempfile
from typing import Mapping

from argus.bounded_process import BoundedProcess, BoundedProcessConfig, ProcessOutcome
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerOutcome, WorkerProtocolError, WorkerRequest, parse_worker_response

_PROTOCOL_PROMPT = """ARGUS bounded worker protocol v1.
Read the attached JSON request file. Execute only the requested bounded operation in the configured project context.
Return exactly one JSON object on stdout and no Markdown/code fences. The object must use this schema:
{
  "schema_version": 1,
  "request_id": "<same request_id>",
  "outcome": "success|retryable_failure|permanent_failure",
  "output": {},
  "message": null,
  "retry_after_seconds": null,
  "usage": null
}
Do not invent a different schema. If the operation cannot be completed safely, use retryable_failure or permanent_failure explicitly.
"""


@dataclass(frozen=True)
class OpenCodeConfig:
    model: str
    executable: str = "opencode"
    working_directory: str | None = None
    agent: str | None = None
    variant: str | None = None
    attach: str | None = None
    timeout_seconds: float = 900.0
    termination_grace_seconds: float = 2.0
    max_stdout_bytes: int = 1_048_576
    max_stderr_bytes: int = 131_072
    extra_args: tuple[str, ...] = ()
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("OpenCode model must be explicit for bounded non-interactive execution")
        if not self.executable:
            raise ValueError("OpenCode executable must be non-empty")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if self.working_directory is not None and not self.working_directory:
            raise ValueError("working_directory must be non-empty when provided")


class OpenCodeWorkerAdapter:
    """Invoke `opencode run` and require a strict ARGUS WorkerResponse."""

    def __init__(self, config: OpenCodeConfig) -> None:
        self.config = config

    def invoke(self, request: WorkerRequest) -> WorkerInvocation:
        with tempfile.TemporaryDirectory(prefix="argus-opencode-") as temporary_directory:
            request_path = Path(temporary_directory) / "argus-worker-request.json"
            request_path.write_text(request.to_json() + "\n", encoding="utf-8")
            try:
                request_path.chmod(0o600)
            except OSError:
                # Permission hardening may not be available on every platform. The file
                # is still isolated in a private TemporaryDirectory and removed eagerly.
                pass

            command = self.build_command(request_path)
            process = BoundedProcess(
                BoundedProcessConfig(
                    command=command,
                    cwd=self.config.working_directory,
                    timeout_seconds=self.config.timeout_seconds,
                    termination_grace_seconds=self.config.termination_grace_seconds,
                    max_stdout_bytes=self.config.max_stdout_bytes,
                    max_stderr_bytes=self.config.max_stderr_bytes,
                    env=self.config.env,
                )
            ).run()

        if process.outcome is not ProcessOutcome.COMPLETED:
            return _transport_failure(process)

        try:
            response = parse_worker_response(
                process.stdout,
                expected_request_id=request.request_id,
            )
        except WorkerProtocolError as exc:
            return WorkerInvocation(
                outcome=InvocationOutcome.MALFORMED_OUTPUT,
                duration_ms=process.duration_ms,
                exit_code=process.exit_code,
                stderr_excerpt=process.stderr_excerpt,
                detail=str(exc),
            )

        outcome = {
            WorkerOutcome.SUCCESS: InvocationOutcome.SUCCESS,
            WorkerOutcome.RETRYABLE_FAILURE: InvocationOutcome.RETRYABLE_FAILURE,
            WorkerOutcome.PERMANENT_FAILURE: InvocationOutcome.PERMANENT_FAILURE,
        }[response.outcome]
        return WorkerInvocation(
            outcome=outcome,
            duration_ms=process.duration_ms,
            response=response,
            exit_code=process.exit_code,
            stderr_excerpt=process.stderr_excerpt,
        )

    def build_command(self, request_path: str | Path) -> tuple[str, ...]:
        """Build the non-interactive OpenCode command without embedding request payloads in argv."""
        command: list[str] = [self.config.executable, "run", "--model", self.config.model]
        if self.config.agent:
            command.extend(["--agent", self.config.agent])
        if self.config.variant:
            command.extend(["--variant", self.config.variant])
        if self.config.attach:
            command.extend(["--attach", self.config.attach])
        if self.config.working_directory:
            command.extend(["--dir", self.config.working_directory])
        command.extend(self.config.extra_args)
        # The positional prompt comes before --file because OpenCode's run message is variadic.
        # BoundedProcess closes stdin, preventing inherited-terminal hangs in non-interactive runs.
        command.append(_PROTOCOL_PROMPT)
        command.extend(["--file", str(request_path)])
        return tuple(command)


def _transport_failure(process) -> WorkerInvocation:
    mapping = {
        ProcessOutcome.PROCESS_ERROR: InvocationOutcome.PROCESS_ERROR,
        ProcessOutcome.TIMEOUT: InvocationOutcome.TIMEOUT,
        ProcessOutcome.OUTPUT_LIMIT: InvocationOutcome.OUTPUT_LIMIT,
        ProcessOutcome.TERMINATION_UNCERTAIN: InvocationOutcome.TERMINATION_UNCERTAIN,
    }
    return WorkerInvocation(
        outcome=mapping[process.outcome],
        duration_ms=process.duration_ms,
        exit_code=process.exit_code,
        stderr_excerpt=process.stderr_excerpt,
        detail=process.detail,
    )
