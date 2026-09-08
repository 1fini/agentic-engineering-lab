from __future__ import annotations

import json

from argus.cli import main
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _create_mission(path) -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.work",
                    payload={"value": 1},
                )
            ],
        )


def test_schedule_due_status_and_inspect_are_machine_readable(tmp_path, capsys) -> None:
    store = tmp_path / "argus.db"
    _create_mission(store)

    assert (
        main(
            [
                "schedule",
                "--store",
                str(store),
                "--due-at",
                "2026-09-08T13:00:00+02:00",
                "mission-1",
                "step-0",
            ]
        )
        == 0
    )
    scheduled = json.loads(capsys.readouterr().out)
    assert scheduled["due_at"] == "2026-09-08T11:00:00.000000Z"

    assert main(["due", "--store", str(store), "--at", "2026-09-08T10:59:59Z"]) == 0
    assert json.loads(capsys.readouterr().out)["steps"] == []

    assert main(["due", "--store", str(store), "--at", "2026-09-08T11:00:00Z"]) == 0
    due = json.loads(capsys.readouterr().out)
    assert due["steps"] == [
        {
            "mission_id": "mission-1",
            "step_id": "step-0",
            "due_at": "2026-09-08T11:00:00.000000Z",
        }
    ]

    assert main(["status", "--store", str(store), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["steps"][0]["due_at"] == "2026-09-08T11:00:00.000000Z"

    assert main(["inspect", "--store", str(store), "mission-1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["steps"][0]["due_at"] == "2026-09-08T11:00:00.000000Z"
    assert inspected["schedule_history"][0]["event_type"] == "scheduled"


def test_invalid_due_at_returns_state_error(tmp_path, capsys) -> None:
    store = tmp_path / "argus.db"
    _create_mission(store)

    assert (
        main(
            [
                "schedule",
                "--store",
                str(store),
                "--due-at",
                "2026-09-08T11:00:00",
                "mission-1",
                "step-0",
            ]
        )
        == 3
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "state_error"
    assert "timezone" in payload["message"]
