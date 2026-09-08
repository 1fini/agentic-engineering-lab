from __future__ import annotations

import json

from argus.cli import main
from argus.effects import EffectIntent, EffectStore
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _prepare(path) -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.effect",
                    payload={"secretish_ref": "consumer://private-step-input"},
                )
            ],
        )
    with EffectStore(path) as effects:
        effects.create_intent(
            EffectIntent(
                mission_id="mission-1",
                step_id="step-0",
                effect_id="effect-0",
                operation="external.apply",
                payload={"secretish_ref": "consumer://private-effect-input"},
            )
        )
        effects.begin_execution("mission-1", "step-0", "effect-0")


def test_status_reports_effect_count_state_and_attempt_count_without_payload(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    assert main(["status", "--store", str(path), "mission-1"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["steps"][0]["effect_count"] == 1
    assert payload["steps"][0]["effect_states"] == ["outcome_unknown"]
    assert payload["steps"][0]["effect_attempt_count"] == 1
    assert "consumer://private" not in captured.out


def test_inspect_reports_effect_identity_attempt_lineage_and_history_without_payload(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    assert main(["inspect", "--store", str(path), "mission-1"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["effects"][0]["effect_id"] == "effect-0"
    assert payload["effects"][0]["state"] == "outcome_unknown"
    assert payload["effects"][0]["correlation_key"].startswith("argus:effect:v1:")
    assert payload["effect_attempts"][0]["attempt_no"] == 1
    assert payload["effect_attempts"][0]["attempt_key"].endswith(":attempt:1")
    assert payload["effect_attempts"][0]["state"] == "outcome_unknown"
    assert [item["event_type"] for item in payload["effect_history"]] == [
        "intent_committed",
        "execution_boundary_entered",
    ]
    assert "consumer://private" not in captured.out
