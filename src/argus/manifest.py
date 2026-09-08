"""Versioned JSON mission manifest loading for the Phase 1 CLI."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping

from argus.model import CURRENT_SCHEMA_VERSION, StepEnvelope


class ManifestError(ValueError):
    """Raised when a mission manifest violates the public Phase 1 contract."""


@dataclass(frozen=True)
class MissionManifest:
    mission_id: str
    steps: tuple[StepEnvelope, ...]
    schema_version: int = CURRENT_SCHEMA_VERSION


def load_manifest(path: str | Path) -> MissionManifest:
    source = Path(path)
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read mission manifest: {source}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"mission manifest is not valid JSON: {source}") from exc

    if not isinstance(raw, dict):
        raise ManifestError("mission manifest root must be a JSON object")
    schema_version = raw.get("schema_version")
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ManifestError(
            f"unsupported mission manifest schema version: {schema_version!r}"
        )
    mission_id = raw.get("mission_id")
    if not isinstance(mission_id, str) or not mission_id:
        raise ManifestError("mission_id must be a non-empty string")
    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raise ManifestError("steps must be a JSON array")

    steps: list[StepEnvelope] = []
    for ordinal, raw_step in enumerate(raw_steps):
        steps.append(_parse_step(mission_id, ordinal, raw_step))

    return MissionManifest(
        mission_id=mission_id,
        steps=tuple(steps),
        schema_version=schema_version,
    )


def _parse_step(mission_id: str, ordinal: int, raw: Any) -> StepEnvelope:
    if not isinstance(raw, dict):
        raise ManifestError(f"step at ordinal {ordinal} must be a JSON object")
    step_id = raw.get("step_id")
    operation = raw.get("operation")
    payload = raw.get("payload", {})
    payload_version = raw.get("payload_version", 1)
    if not isinstance(step_id, str) or not step_id:
        raise ManifestError(f"step at ordinal {ordinal} has invalid step_id")
    if not isinstance(operation, str) or not operation:
        raise ManifestError(f"step {step_id!r} has invalid operation")
    if not isinstance(payload, dict):
        raise ManifestError(f"step {step_id!r} payload must be a JSON object")
    if not isinstance(payload_version, int) or isinstance(payload_version, bool):
        raise ManifestError(f"step {step_id!r} payload_version must be an integer")
    try:
        return StepEnvelope(
            mission_id=mission_id,
            step_id=step_id,
            ordinal=ordinal,
            operation=operation,
            payload=payload,
            payload_version=payload_version,
        )
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"invalid step {step_id!r}: {exc}") from exc
