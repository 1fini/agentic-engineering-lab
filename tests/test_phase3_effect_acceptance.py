from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from argus.effect_attempts import EffectAttemptState, EffectAttemptStore
from argus.effect_runtime import (
    EffectExecutionOutcome,
    EffectExecutionResult,
    EffectRuntime,
    EffectRuntimeDecision,
    ReconciliationDecision,
    ReconciliationResult,
)
from argus.effects import (
    AmbiguousEffectError,
    EffectError,
    EffectIntent,
    EffectState,
    EffectStore,
)
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _prepare(path: Path) -> EffectIntent:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.effect",
                    payload={
                        "cycle_id": "cycle-001",
                        "artifact_ref": "consumer://artifact/candidate",
                    },
                )
            ],
        )
    intent = EffectIntent(
        mission_id="mission-1",
        step_id="step-0",
        effect_id="effect-0",
        operation="external.apply",
        payload={
            "cycle_id": "cycle-001",
            "artifact_ref": "consumer://artifact/candidate",
            "policy_ref": "consumer://policy/effect-v1",
        },
    )
    with EffectStore(path) as effects:
        effects.create_intent(intent)
    return intent


def _remote_records(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _append_remote(path: Path, *, correlation_key: str, attempt_key: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "correlation_key": correlation_key,
                    "attempt_key": attempt_key,
                },
                sort_keys=True,
            )
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


@dataclass
class _RecordingExecutor:
    remote_path: Path
    outcome: EffectExecutionOutcome = EffectExecutionOutcome.APPLIED
    calls: int = 0

    def execute(self, context):
        self.calls += 1
        if self.outcome is EffectExecutionOutcome.APPLIED:
            _append_remote(
                self.remote_path,
                correlation_key=context.effect.intent.correlation_key,
                attempt_key=context.attempt.attempt_key,
            )
        return EffectExecutionResult(
            correlation_key=context.effect.intent.correlation_key,
            attempt_key=context.attempt.attempt_key,
            outcome=self.outcome,
            evidence={"remote_ref": f"remote://attempt/{context.attempt.attempt_no}"},
        )


@dataclass
class _RemoteLogReconciler:
    remote_path: Path
    absent_decision: ReconciliationDecision
    calls: int = 0

    def reconcile(self, context):
        self.calls += 1
        records = _remote_records(self.remote_path)
        applied = any(
            item.get("correlation_key") == context.effect.intent.correlation_key
            and item.get("attempt_key") == context.attempt.attempt_key
            for item in records
        )
        decision = (
            ReconciliationDecision.CONFIRMED_APPLIED
            if applied
            else self.absent_decision
        )
        return ReconciliationResult(
            correlation_key=context.effect.intent.correlation_key,
            attempt_key=context.attempt.attempt_key,
            decision=decision,
            evidence={"lookup_ref": f"evidence://attempt/{context.attempt.attempt_no}"},
        )


def _run_crash_script(script: str, *args: Path, expected_code: int) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script), *[str(arg) for arg in args]],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == expected_code, completed.stderr


def test_crash_after_intent_before_effect_call_is_safe_to_resume(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    script = r"""
        import os, sys
        from argus.effects import EffectIntent, EffectStore
        from argus.model import StepEnvelope
        from argus.store import SqliteMissionStore

        db = sys.argv[1]
        with SqliteMissionStore(db) as store:
            store.create_mission(
                "mission-1",
                [StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.effect",
                    payload={"artifact_ref": "consumer://artifact/candidate"},
                )],
            )
        with EffectStore(db) as effects:
            effects.create_intent(EffectIntent(
                mission_id="mission-1",
                step_id="step-0",
                effect_id="effect-0",
                operation="external.apply",
                payload={"artifact_ref": "consumer://artifact/candidate"},
            ))
        os._exit(81)
    """
    _run_crash_script(script, path, expected_code=81)

    with EffectStore(path) as effects:
        effect = effects.load("mission-1", "step-0", "effect-0")
        assert effect.state is EffectState.INTENT_COMMITTED
    assert _remote_records(remote) == []

    executor = _RecordingExecutor(remote)
    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", executor
    )
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert executor.calls == 1
    assert len(_remote_records(remote)) == 1


def test_crash_after_remote_acceptance_before_receipt_requires_reconciliation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    _prepare(path)
    script = r"""
        import json, os, sys
        from argus.effect_runtime import EffectRuntime

        db, remote = sys.argv[1], sys.argv[2]

        class CrashAfterRemoteApply:
            def execute(self, context):
                with open(remote, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps({
                        "correlation_key": context.effect.intent.correlation_key,
                        "attempt_key": context.attempt.attempt_key,
                    }, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os._exit(86)

        EffectRuntime(db).execute_effect(
            "mission-1", "step-0", "effect-0", CrashAfterRemoteApply()
        )
    """
    _run_crash_script(script, path, remote, expected_code=86)

    records = _remote_records(remote)
    assert len(records) == 1
    with EffectStore(path) as effects:
        effect = effects.load("mission-1", "step-0", "effect-0")
        assert effect.state is EffectState.OUTCOME_UNKNOWN
    with EffectAttemptStore(path) as attempts:
        latest = attempts.latest_attempt("mission-1", "step-0", "effect-0")
        assert latest is not None
        assert latest.state is EffectAttemptState.OUTCOME_UNKNOWN

    # A restarted runtime cannot replay the external call.
    replay = _RecordingExecutor(remote)
    with pytest.raises(AmbiguousEffectError, match="reconcile before replay"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", replay
        )
    assert replay.calls == 0
    assert len(_remote_records(remote)) == 1

    reconciler = _RemoteLogReconciler(
        remote,
        absent_decision=ReconciliationDecision.STILL_UNKNOWN,
    )
    reconciled = EffectRuntime(path).reconcile_effect(
        "mission-1", "step-0", "effect-0", reconciler
    )
    assert reconciled.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert reconciled.attempt.state is EffectAttemptState.CONFIRMED_APPLIED
    assert reconciler.calls == 1
    assert len(_remote_records(remote)) == 1

    # Further restarts remain terminal and cannot duplicate the remote effect.
    with pytest.raises(EffectError, match="terminal state"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", replay
        )
    assert len(_remote_records(remote)) == 1


def test_crash_before_remote_application_reconciles_not_applied_then_allows_one_reattempt(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    _prepare(path)
    script = r"""
        import os, sys
        from argus.effect_runtime import EffectRuntime

        db = sys.argv[1]

        class CrashBeforeRemoteApply:
            def execute(self, context):
                os._exit(85)

        EffectRuntime(db).execute_effect(
            "mission-1", "step-0", "effect-0", CrashBeforeRemoteApply()
        )
    """
    _run_crash_script(script, path, expected_code=85)
    assert _remote_records(remote) == []

    reconciler = _RemoteLogReconciler(
        remote,
        absent_decision=ReconciliationDecision.CONFIRMED_NOT_APPLIED,
    )
    first = EffectRuntime(path).reconcile_effect(
        "mission-1", "step-0", "effect-0", reconciler
    )
    assert first.decision is EffectRuntimeDecision.CONFIRMED_NOT_APPLIED
    assert first.attempt.attempt_no == 1

    executor = _RecordingExecutor(remote)
    second = EffectRuntime(path).reattempt_effect(
        "mission-1", "step-0", "effect-0", executor
    )
    assert second.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    assert second.attempt.attempt_no == 2
    assert executor.calls == 1
    records = _remote_records(remote)
    assert len(records) == 1
    assert records[0]["attempt_key"].endswith(":attempt:2")

    with EffectAttemptStore(path) as attempts:
        lineage = attempts.list_attempts("mission-1", "step-0", "effect-0")
    assert [item.state for item in lineage] == [
        EffectAttemptState.CONFIRMED_NOT_APPLIED,
        EffectAttemptState.CONFIRMED_APPLIED,
    ]


def test_still_unknown_reconciliation_remains_blocked_across_restarts(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    _prepare(path)
    runtime = EffectRuntime(path)
    unknown_executor = _RecordingExecutor(remote, outcome=EffectExecutionOutcome.UNKNOWN)
    initial = runtime.execute_effect(
        "mission-1", "step-0", "effect-0", unknown_executor
    )
    assert initial.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
    assert _remote_records(remote) == []

    reconciler = _RemoteLogReconciler(
        remote,
        absent_decision=ReconciliationDecision.STILL_UNKNOWN,
    )
    for _ in range(2):
        result = EffectRuntime(path).reconcile_effect(
            "mission-1", "step-0", "effect-0", reconciler
        )
        assert result.decision is EffectRuntimeDecision.BLOCKED_UNKNOWN
        with pytest.raises(AmbiguousEffectError):
            EffectRuntime(path).reattempt_effect(
                "mission-1", "step-0", "effect-0", _RecordingExecutor(remote)
            )
    assert reconciler.calls == 2
    assert _remote_records(remote) == []
    with EffectAttemptStore(path) as attempts:
        assert len(attempts.list_attempts("mission-1", "step-0", "effect-0")) == 1


def test_crash_after_parent_receipt_before_lineage_sync_repairs_without_reexecution(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    _prepare(path)
    script = r"""
        import json, os, sys
        from argus.effect_attempts import EffectAttemptStore
        from argus.effects import (
            EffectReceipt, EffectReceiptOutcome, EffectReceiptSource, EffectStore
        )

        db, remote = sys.argv[1], sys.argv[2]
        with EffectStore(db) as effects:
            pending = effects.begin_execution("mission-1", "step-0", "effect-0")
        with EffectAttemptStore(db) as attempts:
            attempt = attempts.ensure_current_attempt(pending)
        with open(remote, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "correlation_key": pending.intent.correlation_key,
                "attempt_key": attempt.attempt_key,
            }, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        with EffectStore(db) as effects:
            effects.record_receipt(
                "mission-1",
                "step-0",
                "effect-0",
                EffectReceipt(
                    correlation_key=pending.intent.correlation_key,
                    outcome=EffectReceiptOutcome.APPLIED,
                    source=EffectReceiptSource.EXECUTION,
                    evidence={"remote_ref": "remote://opaque"},
                ),
            )
        # Die before EffectRuntime can synchronize the dedicated attempt lineage.
        os._exit(87)
    """
    _run_crash_script(script, path, remote, expected_code=87)
    assert len(_remote_records(remote)) == 1

    with EffectStore(path) as effects:
        effect = effects.load("mission-1", "step-0", "effect-0")
        assert effect.state is EffectState.CONFIRMED_APPLIED
    # Opening the attempt store repairs only local lineage from the authoritative receipt.
    with EffectAttemptStore(path) as attempts:
        latest = attempts.latest_attempt("mission-1", "step-0", "effect-0")
        assert latest is not None
        assert latest.state is EffectAttemptState.CONFIRMED_APPLIED

    replay = _RecordingExecutor(remote)
    with pytest.raises(EffectError, match="terminal state"):
        EffectRuntime(path).execute_effect(
            "mission-1", "step-0", "effect-0", replay
        )
    assert replay.calls == 0
    assert len(_remote_records(remote)) == 1


def test_restarted_inspect_exposes_effect_lineage_without_raw_payloads(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote.log"
    _prepare(path)
    result = EffectRuntime(path).execute_effect(
        "mission-1", "step-0", "effect-0", _RecordingExecutor(remote)
    )
    assert result.decision is EffectRuntimeDecision.CONFIRMED_APPLIED

    completed = subprocess.run(
        [sys.executable, "-m", "argus.cli", "inspect", "--store", str(path), "mission-1"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["effects"][0]["state"] == "confirmed_applied"
    assert payload["effect_attempts"][0]["state"] == "confirmed_applied"
    assert payload["effect_attempts"][0]["attempt_key"].endswith(":attempt:1")
    assert "consumer://artifact/candidate" not in completed.stdout
    assert "remote://attempt" not in completed.stdout


def test_reference_effect_fixture_is_domain_opaque() -> None:
    fixture = (
        Path(__file__).resolve().parents[1]
        / "examples"
        / "reference-effect-workload-v1.json"
    )
    payload = json.loads(fixture.read_text())
    assert payload["effect"]["operation"] == "external.apply"
    material = fixture.read_text().lower()
    forbidden = (
        "youtube",
        "short",
        "retention",
        "editorial",
        "hook",
        "publication",
    )
    assert not any(term in material for term in forbidden)
