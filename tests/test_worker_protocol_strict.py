from __future__ import annotations

import math

import pytest

from argus.worker_protocol import WorkerProtocolError, WorkerRequest, WorkerUsage, parse_worker_response


def test_request_rejects_non_finite_payload_numbers() -> None:
    with pytest.raises(WorkerProtocolError, match="strict JSON"):
        WorkerRequest(request_id="req-1", operation="fixture.work", payload={"value": math.nan})


def test_response_rejects_non_finite_json_numbers() -> None:
    with pytest.raises(WorkerProtocolError, match="non-finite"):
        parse_worker_response(
            '{"schema_version":1,"request_id":"req-1","outcome":"success","output":{"value":NaN}}',
            expected_request_id="req-1",
        )


def test_usage_rejects_infinite_cost() -> None:
    with pytest.raises(WorkerProtocolError, match="finite"):
        WorkerUsage(cost_usd=math.inf)
