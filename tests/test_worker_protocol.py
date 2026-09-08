from __future__ import annotations

import json

import pytest

from argus.worker_protocol import (
    WorkerOutcome,
    WorkerProtocolError,
    WorkerRequest,
    WorkerResponse,
    WorkerUsage,
    parse_worker_response,
)


def test_worker_request_is_canonical_json() -> None:
    request = WorkerRequest(
        request_id="req-1",
        operation="fixture.work",
        payload={"b": 2, "a": 1},
    )
    assert request.to_json() == (
        '{"operation":"fixture.work","payload":{"a":1,"b":2},'
        '"payload_version":1,"request_id":"req-1","schema_version":1}'
    )


def test_parse_success_response_with_usage() -> None:
    response = parse_worker_response(
        json.dumps(
            {
                "schema_version": 1,
                "request_id": "req-1",
                "outcome": "success",
                "output": {"artifact_ref": "artifact://1"},
                "message": None,
                "retry_after_seconds": None,
                "usage": {
                    "input_tokens": 123,
                    "output_tokens": 45,
                    "cost_usd": 0.0123,
                },
            }
        ),
        expected_request_id="req-1",
    )
    assert response.outcome is WorkerOutcome.SUCCESS
    assert response.usage == WorkerUsage(input_tokens=123, output_tokens=45, cost_usd=0.0123)


def test_retry_after_is_only_valid_for_retryable_failure() -> None:
    with pytest.raises(WorkerProtocolError, match="retry_after_seconds"):
        WorkerResponse(
            request_id="req-1",
            outcome=WorkerOutcome.SUCCESS,
            output={},
            retry_after_seconds=1,
        )


def test_response_rejects_request_mismatch_unknown_fields_and_bad_usage() -> None:
    with pytest.raises(WorkerProtocolError, match="request_id mismatch"):
        parse_worker_response(
            '{"schema_version":1,"request_id":"other","outcome":"success","output":{}}',
            expected_request_id="req-1",
        )

    with pytest.raises(WorkerProtocolError, match="unknown fields"):
        parse_worker_response(
            '{"schema_version":1,"request_id":"req-1","outcome":"success","output":{},"extra":1}',
            expected_request_id="req-1",
        )

    with pytest.raises(WorkerProtocolError, match="input_tokens"):
        parse_worker_response(
            '{"schema_version":1,"request_id":"req-1","outcome":"success","output":{},'
            '"usage":{"input_tokens":-1}}',
            expected_request_id="req-1",
        )


def test_response_rejects_non_json_non_object_and_invalid_outcome() -> None:
    with pytest.raises(WorkerProtocolError, match="valid JSON"):
        parse_worker_response("not-json", expected_request_id="req-1")
    with pytest.raises(WorkerProtocolError, match="root"):
        parse_worker_response("[]", expected_request_id="req-1")
    with pytest.raises(WorkerProtocolError, match="invalid worker outcome"):
        parse_worker_response(
            '{"schema_version":1,"request_id":"req-1","outcome":"maybe","output":{}}',
            expected_request_id="req-1",
        )
