"""Generic bounded worker adapter built on the versioned ARGUS worker protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from argus.bounded_process import BoundedProcess, BoundedProcessConfig, ProcessOutcome
from argus.worker_protocol import (
    WorkerOutcome,
    WorkerProtocolError,
    WorkerRequest,
    WorkerResponse,
    parse_worker_response,
)


class InvocationOutcome(StrEnum):
    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"
    PROCESS_ERROR = "process_error"
    TIMEOUT = "timeout"
    OUTPUT_LIMIT = "output_limit"
    MALFORMED_OUTPUT = "malformed_output"
    TERMINATION_UNCERTAIN = "termination_uncertain"


@dataclass(frozen=True)
class WorkerInvocation:
    outcome: InvocationOutcome
    duration_ms: int
    response: WorkerResponse | None = None
    exit_code: int | None = None
    stderr_excerpt: str | None = None
    detail: str | None = None


class JsonSubprocessWorker:
    """Invoke a worker process that speaks ARGUS worker JSON on stdin/stdout."""

    def __init__(self, config: BoundedProcessConfig) -> None:
        self.config = config

    def invoke(self, request: WorkerRequest) -> WorkerInvocation:
        result = BoundedProcess(self.config).run(request.to_json().encode("utf-8"))
        if result.outcome is not ProcessOutcome.COMPLETED:
            return _transport_failure(result)
        try:
            response = parse_worker_response(
                result.stdout,
                expected_request_id=request.request_id,
            )
        except WorkerProtocolError as exc:
            return WorkerInvocation(
                outcome=InvocationOutcome.MALFORMED_OUTPUT,
                duration_ms=result.duration_ms,
                exit_code=result.exit_code,
                stderr_excerpt=result.stderr_excerpt,
                detail=str(exc),
            )
        return WorkerInvocation(
            outcome=_response_outcome(response.outcome),
            duration_ms=result.duration_ms,
            response=response,
            exit_code=result.exit_code,
            stderr_excerpt=result.stderr_excerpt,
        )


def _response_outcome(outcome: WorkerOutcome) -> InvocationOutcome:
    if outcome is WorkerOutcome.SUCCESS:
        return InvocationOutcome.SUCCESS
    if outcome is WorkerOutcome.RETRYABLE_FAILURE:
        return InvocationOutcome.RETRYABLE_FAILURE
    return InvocationOutcome.PERMANENT_FAILURE


def _transport_failure(result) -> WorkerInvocation:
    mapping = {
        ProcessOutcome.PROCESS_ERROR: InvocationOutcome.PROCESS_ERROR,
        ProcessOutcome.TIMEOUT: InvocationOutcome.TIMEOUT,
        ProcessOutcome.OUTPUT_LIMIT: InvocationOutcome.OUTPUT_LIMIT,
        ProcessOutcome.TERMINATION_UNCERTAIN: InvocationOutcome.TERMINATION_UNCERTAIN,
    }
    return WorkerInvocation(
        outcome=mapping[result.outcome],
        duration_ms=result.duration_ms,
        exit_code=result.exit_code,
        stderr_excerpt=result.stderr_excerpt,
        detail=result.detail,
    )
