from __future__ import annotations

import json

from argus.cli import main


def _write_manifest(path, *, fail: bool = False) -> None:
    operation = "fixture.fail" if fail else "fixture.echo"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mission_id": "mission-1",
                "steps": [
                    {
                        "step_id": f"step-{index}",
                        "operation": operation,
                        "payload": {"index": index},
                    }
                    for index in range(3)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_cli_run_status_inspect_and_repeat_are_machine_readable(tmp_path, capsys) -> None:
    store = tmp_path / "argus.db"
    manifest = tmp_path / "mission.json"
    _write_manifest(manifest)

    assert (
        main(
            [
                "run",
                "--store",
                str(store),
                "--manifest",
                str(manifest),
                "--fixture-workers",
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["state"] == "completed"
    assert first["executed_steps"] == ["step-0", "step-1", "step-2"]

    assert main(["status", "--store", str(store), "mission-1"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["state"] == "completed"
    assert [step["state"] for step in status["steps"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]

    assert main(["inspect", "--store", str(store), "mission-1"]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["state"] == "completed"
    assert inspected["journal"][-1]["to_state"] == "completed"
    assert "payload" not in inspected["steps"][0]
    assert inspected["steps"][0]["idempotency_key"].startswith("argus:v1:")

    assert (
        main(
            [
                "run",
                "--store",
                str(store),
                "--manifest",
                str(manifest),
                "--fixture-workers",
            ]
        )
        == 0
    )
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["executed_steps"] == []


def test_cli_without_worker_registry_returns_blocked_exit(tmp_path, capsys) -> None:
    store = tmp_path / "argus.db"
    manifest = tmp_path / "mission.json"
    _write_manifest(manifest)

    assert main(["run", "--store", str(store), "--manifest", str(manifest)]) == 4
    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert payload["error"] == "blocked"
    assert "no worker registered" in payload["message"]


def test_cli_explicit_fixture_failure_returns_failed_exit(tmp_path, capsys) -> None:
    store = tmp_path / "argus.db"
    manifest = tmp_path / "mission.json"
    _write_manifest(manifest, fail=True)

    assert (
        main(
            [
                "run",
                "--store",
                str(store),
                "--manifest",
                str(manifest),
                "--fixture-workers",
            ]
        )
        == 5
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "failed"


def test_cli_invalid_manifest_returns_input_error(tmp_path, capsys) -> None:
    manifest = tmp_path / "mission.json"
    manifest.write_text('{"schema_version":999}', encoding="utf-8")

    assert (
        main(
            [
                "run",
                "--store",
                str(tmp_path / "argus.db"),
                "--manifest",
                str(manifest),
                "--fixture-workers",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "input_error"


def test_cli_unknown_mission_returns_state_error(tmp_path, capsys) -> None:
    assert main(["status", "--store", str(tmp_path / "argus.db"), "missing"]) == 3
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "state_error"
