from __future__ import annotations

import json

from argus.budgets import BudgetStore
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


def test_budget_init_and_status_are_machine_readable(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    assert (
        main(
            [
                "budget-init",
                "--store",
                str(path),
                "--worker-attempt-limit",
                "3",
                "--effect-attempt-limit",
                "2",
                "--spend-limit-usd",
                "10.50",
                "mission-1",
            ]
        )
        == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["budget"]["worker_attempt_limit"] == 3
    assert initialized["budget"]["effect_attempt_limit"] == 2
    assert initialized["budget"]["spend_limit_usd"] == "10.50"
    assert initialized["budget"]["spend_available_usd"] == "10.50"
    assert initialized["policy_hash"].startswith("argus:budget:v1:")

    assert main(["status", "--store", str(path), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["budget"]["worker_attempts_used"] == 0
    assert status["budget"]["effect_attempts_used"] == 0
    assert status["budget"]["spend_reserved_usd"] == "0"
    assert "artifact://private-input" not in json.dumps(status)


def test_inspect_exposes_reservation_and_budget_history_without_payload(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)
    assert (
        main(
            [
                "budget-init",
                "--store",
                str(path),
                "--spend-limit-usd",
                "5",
                "mission-1",
            ]
        )
        == 0
    )
    capsys.readouterr()

    with BudgetStore(path) as budgets:
        budgets.reserve_spend("mission-1", "opaque-call-1", "3")

    assert main(["inspect", "--store", str(path), "mission-1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["budget"]["spend_reserved_usd"] == "3"
    assert inspected["budget"]["spend_available_usd"] == "2"
    assert inspected["spend_reservations"][0]["reservation_key"] == "opaque-call-1"
    assert inspected["spend_reservations"][0]["state"] == "reserved"
    assert any(
        event["event_type"] == "spend_reserved"
        and event["reservation_key"] == "opaque-call-1"
        for event in inspected["budget_history"]
    )
    assert "artifact://private-input" not in json.dumps(inspected)


def test_old_mission_without_budget_policy_reports_null_budget(tmp_path, capsys) -> None:
    path = tmp_path / "argus.db"
    _prepare(path)

    assert main(["status", "--store", str(path), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["budget"] is None

    assert main(["inspect", "--store", str(path), "mission-1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["budget"] is None
    assert inspected["spend_reservations"] == []
    assert inspected["budget_history"] == []
