"""Explicit test/demo workers for the Phase 1 CLI.

These workers are intentionally generic fixtures. They are not a production worker
backend and must not grow consumer business semantics.
"""

from __future__ import annotations

from typing import Any, Mapping

from argus.model import StepEnvelope
from argus.runner import StepExecutionFailure, WorkerRegistry


def build_fixture_registry() -> WorkerRegistry:
    registry = WorkerRegistry()
    registry.register("fixture.echo", _echo)
    registry.register("fixture.fail", _fail)
    return registry


def _echo(step: StepEnvelope) -> Mapping[str, Any]:
    return {"echo": dict(step.payload), "step_id": step.step_id}


def _fail(step: StepEnvelope) -> Mapping[str, Any]:
    message = step.payload.get("message", "fixture requested failure")
    raise StepExecutionFailure(str(message), output={"step_id": step.step_id})
