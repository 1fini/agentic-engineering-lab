from __future__ import annotations

import json

from argus.cli import main
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
                    operation="fixture.compute",
                    payload={"input_ref": "artifact://private-input"},
                )
            ],
        )


def test_guardrail_init_pause_status_and_resume(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    assert (
        main(
            [
                "guardrail-init",
                "--store",
                str(path),
                "--workload-scope",
                "workload-alpha",
                "--policy-ref",
                "consumer://private-policy",
                "mission-1",
            ]
        )
        == 0
    )
    init_output = json.loads(capsys.readouterr().out)
    assert init_output["control_state"] == "active"
    assert init_output["gate_decision"] == "allow"
    assert "consumer://private-policy" not in json.dumps(init_output)

    assert main(["control", "--store", str(path), "pause", "mission-1"]) == 0
    paused = json.loads(capsys.readouterr().out)
    assert paused["control_state"] == "paused"
    assert paused["gate_reason"] == "mission_paused"

    assert main(["status", "--store", str(path), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["guardrail"]["control_state"] == "paused"
    assert status["guardrail"]["gate_decision"] == "deny"

    assert main(["control", "--store", str(path), "resume", "mission-1"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["control_state"] == "active"
    assert resumed["gate_decision"] == "allow"


def test_global_kill_gate_and_inspect_history(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    assert (
        main(
            [
                "guardrail-init",
                "--store",
                str(path),
                "--workload-scope",
                "workload-alpha",
                "mission-1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["kill-switch", "--store", str(path), "--global-scope", "on"]) == 0
    kill_output = json.loads(capsys.readouterr().out)
    assert kill_output["enabled"] is True
    assert kill_output["scope_type"] == "global"

    assert main(["control", "--store", str(path), "gate", "mission-1"]) == 0
    gate = json.loads(capsys.readouterr().out)
    assert gate["gate_reason"] == "global_kill"

    assert main(["inspect", "--store", str(path), "mission-1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["guardrail"]["global_kill"] is True
    assert any(
        event["event_type"] == "global_kill_enabled"
        for event in inspected["guardrail_history"]
    )
    assert any(
        event["event_type"] == "gate_decision"
        and event["reason"] == "global_kill"
        for event in inspected["guardrail_history"]
    )
    serialized = json.dumps(inspected)
    assert "artifact://private-input" not in serialized


def test_cancel_is_terminal_in_cli(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    assert (
        main(
            [
                "guardrail-init",
                "--store",
                str(path),
                "--workload-scope",
                "workload-alpha",
                "mission-1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert main(["control", "--store", str(path), "cancel", "mission-1"]) == 0
    cancelled = json.loads(capsys.readouterr().out)
    assert cancelled["control_state"] == "cancelled"
    assert cancelled["gate_reason"] == "mission_cancelled"

    assert main(["control", "--store", str(path), "resume", "mission-1"]) == 3
    error = json.loads(capsys.readouterr().err)
    assert error["error"] == "state_error"
    assert "cannot be resumed" in error["message"]
