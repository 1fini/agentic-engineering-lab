"""Versioned generic worker request/response contracts for ARGUS Phase 2."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from typing import Any, Mapping

WORKER_PROTOCOL_VERSION = 1


class WorkerProtocolError(ValueError):
    """Raised when untrusted worker protocol data violates the public contract."""


class WorkerOutcome(StrEnum):
    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    PERMANENT_FAILURE = "permanent_failure"


@dataclass(frozen=True)
class WorkerUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
        ):
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise WorkerProtocolError(f"{name} must be a non-negative integer")
        if self.cost_usd is not None:
            if isinstance(self.cost_usd, bool) or not isinstance(self.cost_usd, (int, float)):
                raise WorkerProtocolError("cost_usd must be a non-negative number")
            if self.cost_usd < 0:
                raise WorkerProtocolError("cost_usd must be a non-negative number")

    def as_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": float(self.cost_usd) if self.cost_usd is not None else None,
        }


@dataclass(frozen=True)
class WorkerRequest:
    request_id: str
    operation: str
    payload: Mapping[str, Any]
    payload_version: int = 1
    schema_version: int = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != WORKER_PROTOCOL_VERSION:
            raise WorkerProtocolError(
                f"unsupported worker request schema version: {self.schema_version}"
            )
        if not self.request_id or not self.operation:
            raise WorkerProtocolError("request_id and operation must be non-empty")
        if self.payload_version < 1:
            raise WorkerProtocolError("payload_version must be >= 1")
        _canonical_json(self.payload)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "operation": self.operation,
            "payload_version": self.payload_version,
            "payload": dict(self.payload),
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


@dataclass(frozen=True)
class WorkerResponse:
    request_id: str
    outcome: WorkerOutcome
    output: Mapping[str, Any]
    message: str | None = None
    retry_after_seconds: float | None = None
    usage: WorkerUsage | None = None
    schema_version: int = WORKER_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != WORKER_PROTOCOL_VERSION:
            raise WorkerProtocolError(
                f"unsupported worker response schema version: {self.schema_version}"
            )
        if not self.request_id:
            raise WorkerProtocolError("request_id must be non-empty")
        _canonical_json(self.output)
        if self.message is not None and not isinstance(self.message, str):
            raise WorkerProtocolError("message must be a string or null")
        if self.retry_after_seconds is not None:
            if isinstance(self.retry_after_seconds, bool) or not isinstance(
                self.retry_after_seconds, (int, float)
            ):
                raise WorkerProtocolError("retry_after_seconds must be a non-negative number")
            if self.retry_after_seconds < 0:
                raise WorkerProtocolError("retry_after_seconds must be a non-negative number")
            if self.outcome is not WorkerOutcome.RETRYABLE_FAILURE:
                raise WorkerProtocolError(
                    "retry_after_seconds is valid only for retryable_failure"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "outcome": self.outcome.value,
            "output": dict(self.output),
            "message": self.message,
            "retry_after_seconds": self.retry_after_seconds,
            "usage": self.usage.as_dict() if self.usage else None,
        }

    def to_json(self) -> str:
        return _canonical_json(self.as_dict())


def parse_worker_response(raw: str | bytes, *, expected_request_id: str) -> WorkerResponse:
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkerProtocolError("worker response is not valid UTF-8") from exc
    elif isinstance(raw, str):
        text = raw
    else:
        raise WorkerProtocolError("worker response must be str or bytes")

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise WorkerProtocolError("worker response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker response root must be a JSON object")

    allowed = {
        "schema_version",
        "request_id",
        "outcome",
        "output",
        "message",
        "retry_after_seconds",
        "usage",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise WorkerProtocolError(f"worker response contains unknown fields: {unknown}")

    if value.get("schema_version") != WORKER_PROTOCOL_VERSION:
        raise WorkerProtocolError(
            f"unsupported worker response schema version: {value.get('schema_version')!r}"
        )
    request_id = value.get("request_id")
    if request_id != expected_request_id:
        raise WorkerProtocolError(
            f"worker response request_id mismatch: expected {expected_request_id!r}, got {request_id!r}"
        )
    try:
        outcome = WorkerOutcome(value.get("outcome"))
    except ValueError as exc:
        raise WorkerProtocolError(f"invalid worker outcome: {value.get('outcome')!r}") from exc

    output = value.get("output")
    if not isinstance(output, dict):
        raise WorkerProtocolError("worker response output must be a JSON object")

    usage_value = value.get("usage")
    usage: WorkerUsage | None
    if usage_value is None:
        usage = None
    elif isinstance(usage_value, dict):
        allowed_usage = {"input_tokens", "output_tokens", "cost_usd"}
        unknown_usage = sorted(set(usage_value) - allowed_usage)
        if unknown_usage:
            raise WorkerProtocolError(
                f"worker usage contains unknown fields: {unknown_usage}"
            )
        usage = WorkerUsage(
            input_tokens=usage_value.get("input_tokens"),
            output_tokens=usage_value.get("output_tokens"),
            cost_usd=usage_value.get("cost_usd"),
        )
    else:
        raise WorkerProtocolError("worker usage must be an object or null")

    return WorkerResponse(
        request_id=request_id,
        outcome=outcome,
        output=output,
        message=value.get("message"),
        retry_after_seconds=value.get("retry_after_seconds"),
        usage=usage,
        schema_version=WORKER_PROTOCOL_VERSION,
    )


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WorkerProtocolError("worker payload must be JSON-serializable") from exc
