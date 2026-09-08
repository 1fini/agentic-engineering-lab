from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys

import pytest

from argus.attempts import AttemptStore
from argus.budgets import BudgetPolicy, BudgetStore, ReservationState
from argus.effect_attempts import EffectAttemptStore
from argus.effect_runtime import (
    EffectRuntime,
    EffectRuntimeDecision,
    ReconciliationDecision,
    ReconciliationResult,
)
from argus.effects import EffectIntent, EffectState, EffectStore
from argus.governed_runtime import GovernedRuntime
from argus.guardrails import GuardrailPolicy, GuardrailStore
from argus.model import StepEnvelope
from argus.scheduler import DurableScheduler
from argus.store import SqliteMissionStore

_DUE = datetime(2030, 1, 2, 12, 0, tzinfo=timezone.utc)


def _prepare(
    path: Path,
    *,
    worker_limit: int | None = None,
    effect_limit: int | None = None,
    spend_limit: str | None = None,
) -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            "mission-1",
            [
                StepEnvelope(
                    mission_id="mission-1",
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.remote",
                    payload={"artifact_ref": "artifact://opaque/input"},
                )
            ],
        )
    with DurableScheduler(path) as scheduler:
        scheduler.schedule("mission-1", "step-0", _DUE)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(
            GuardrailPolicy(
                mission_id="mission-1",
                workload_scope="workload-alpha",
            )
        )
    with BudgetStore(path) as budgets:
        budgets.set_policy(
            BudgetPolicy(
                mission_id="mission-1",
                worker_attempt_limit=worker_limit,
                effect_attempt_limit=effect_limit,
                spend_limit_usd=spend_limit,
            )
        )


def _run_python(code: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code, *(str(arg) for arg in args)],
        capture_output=True,
        text=True,
        check=False,
    )


_WORKER_CHILD = r'''
import pathlib, sys
from argus.governed_runtime import GovernedRuntime, ExecutionGateDenied
from argus.attempts import AmbiguousAttemptError
from argus.worker_adapter import InvocationOutcome, WorkerInvocation
from argus.worker_protocol import WorkerOutcome, WorkerResponse, WorkerUsage

path, marker, reserve = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
class Worker:
    def invoke(self, request):
        count = int(marker.read_text()) if marker.exists() else 0
        marker.write_text(str(count + 1))
        return WorkerInvocation(
            outcome=InvocationOutcome.SUCCESS,
            duration_ms=5,
            exit_code=0,
            response=WorkerResponse(
                request_id=request.request_id,
                outcome=WorkerOutcome.SUCCESS,
                output={'artifact_ref': 'artifact://opaque/result'},
                usage=WorkerUsage(input_tokens=2, output_tokens=1, cost_usd=0.4),
            ),
        )
try:
    kwargs = {} if reserve == '-' else {'reserve_spend_usd': reserve}
    GovernedRuntime(path).execute_due_worker(
        'mission-1', 'step-0', Worker(), now='2030-01-02T12:00:00Z', **kwargs
    )
except ExecutionGateDenied as exc:
    print('denied:' + exc.reason)
    raise SystemExit(23)
except AmbiguousAttemptError:
    print('ambiguous')
    raise SystemExit(24)
'''


def test_pause_resume_and_cancel_survive_real_process_restart(tmp_path) -> None:
    path = tmp_path / "argus.db"
    marker = tmp_path / "worker-count.txt"
    _prepare(path, worker_limit=2)

    with GuardrailStore(path) as guardrails:
        guardrails.pause("mission-1")
    denied = _run_python(_WORKER_CHILD, path, marker, "-")
    assert denied.returncode == 23
    assert "mission_paused" in denied.stdout
    assert not marker.exists()
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0") == []

    with GuardrailStore(path) as guardrails:
        guardrails.resume("mission-1")
    allowed = _run_python(_WORKER_CHILD, path, marker, "-")
    assert allowed.returncode == 0, allowed.stderr
    assert marker.read_text() == "1"

    # A fresh mission proves terminal cancellation across another interpreter.
    path2 = tmp_path / "cancelled.db"
    marker2 = tmp_path / "cancelled-worker-count.txt"
    _prepare(path2, worker_limit=2)
    with GuardrailStore(path2) as guardrails:
        guardrails.cancel("mission-1")
    cancelled = _run_python(_WORKER_CHILD, path2, marker2, "-")
    assert cancelled.returncode == 23
    assert "mission_cancelled" in cancelled.stdout
    assert not marker2.exists()


def test_workload_and_global_kill_survive_restart_and_prevent_allocation(tmp_path) -> None:
    for mode in ("workload", "global"):
        path = tmp_path / f"{mode}.db"
        marker = tmp_path / f"{mode}-worker.txt"
        _prepare(path, worker_limit=2)
        with GuardrailStore(path) as guardrails:
            if mode == "global":
                guardrails.set_global_kill(True)
            else:
                guardrails.set_workload_kill("workload-alpha", True)
        denied = _run_python(_WORKER_CHILD, path, marker, "-")
        assert denied.returncode == 23
        assert f"{mode}_kill" in denied.stdout
        assert not marker.exists()
        with AttemptStore(path) as attempts:
            assert attempts.list_attempts("mission-1", "step-0") == []


def test_worker_attempt_budget_exhaustion_prevents_real_subprocess_invocation(tmp_path) -> None:
    path = tmp_path / "argus.db"
    marker = tmp_path / "worker.txt"
    _prepare(path, worker_limit=0)
    denied = _run_python(_WORKER_CHILD, path, marker, "-")
    assert denied.returncode == 23
    assert "worker_attempt_exhausted" in denied.stdout
    assert not marker.exists()
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0") == []


def test_crash_after_spend_reservation_before_attempt_reuses_same_capacity(tmp_path) -> None:
    path = tmp_path / "argus.db"
    marker = tmp_path / "worker.txt"
    _prepare(path, worker_limit=2, spend_limit="1")

    crash = r'''
import os, sys
from argus.governed_runtime import GovernedRuntime, ExecutionKind
runtime = GovernedRuntime(sys.argv[1])
runtime._authorize(
    'mission-1', ExecutionKind.WORKER,
    subject_key='step-0', reserve_spend_usd='0.6'
)
os._exit(73)
'''
    crashed = _run_python(crash, path)
    assert crashed.returncode == 73
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RESERVED
        assert reservations[0].reserved_usd == Decimal("0.6")
    with AttemptStore(path) as attempts:
        assert attempts.list_attempts("mission-1", "step-0") == []

    resumed = _run_python(_WORKER_CHILD, path, marker, "0.6")
    assert resumed.returncode == 0, resumed.stderr
    assert marker.read_text() == "1"
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.COMMITTED
        assert reservations[0].committed_usd == Decimal("0.4")
        assert budgets.snapshot("mission-1").spend_available_usd == Decimal("0.6")


def test_crash_after_worker_attempt_keeps_reservation_and_blocks_restart_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    marker = tmp_path / "worker-started.txt"
    _prepare(path, worker_limit=3, spend_limit="2")

    crash_worker = r'''
import os, pathlib, sys
from argus.governed_runtime import GovernedRuntime
class Worker:
    def invoke(self, request):
        marker = pathlib.Path(sys.argv[2])
        marker.write_text('started')
        os._exit(74)
GovernedRuntime(sys.argv[1]).execute_due_worker(
    'mission-1', 'step-0', Worker(), now='2030-01-02T12:00:00Z',
    reserve_spend_usd='0.75'
)
'''
    crashed = _run_python(crash_worker, path, marker)
    assert crashed.returncode == 74
    assert marker.read_text() == "started"
    with AttemptStore(path) as attempts:
        latest = attempts.latest_attempt("mission-1", "step-0")
        assert latest is not None and latest.state.value == "started"
    with BudgetStore(path) as budgets:
        assert len(budgets.list_reservations("mission-1")) == 1
        assert budgets.list_reservations("mission-1")[0].state is ReservationState.RESERVED

    replay = _run_python(_WORKER_CHILD, path, tmp_path / "replay-worker.txt", "0.75")
    assert replay.returncode == 24
    assert "ambiguous" in replay.stdout
    assert not (tmp_path / "replay-worker.txt").exists()
    with BudgetStore(path) as budgets:
        assert len(budgets.list_reservations("mission-1")) == 1


def test_effect_remote_acceptance_crash_requires_phase3_reconciliation_before_replay(tmp_path) -> None:
    path = tmp_path / "argus.db"
    remote = tmp_path / "remote-state.txt"
    _prepare(path, effect_limit=2, spend_limit="2")
    with EffectStore(path) as effects:
        effects.create_intent(
            EffectIntent(
                mission_id="mission-1",
                step_id="step-0",
                effect_id="effect-0",
                operation="external.apply",
                payload={"artifact_ref": "artifact://opaque/candidate"},
            )
        )

    crash_effect = r'''
import os, pathlib, sys
from argus.governed_runtime import GovernedRuntime
class Executor:
    def execute(self, context):
        pathlib.Path(sys.argv[2]).write_text(context.attempt.attempt_key)
        os._exit(75)
GovernedRuntime(sys.argv[1]).execute_effect(
    'mission-1', 'step-0', 'effect-0', Executor(),
    now='2030-01-02T12:00:00Z', reserve_spend_usd='0.5'
)
'''
    crashed = _run_python(crash_effect, path, remote)
    assert crashed.returncode == 75
    assert remote.exists()
    with EffectStore(path) as effects:
        assert effects.load("mission-1", "step-0", "effect-0").state is EffectState.OUTCOME_UNKNOWN
    with EffectAttemptStore(path) as attempts:
        history = attempts.list_attempts("mission-1", "step-0", "effect-0")
        assert len(history) == 1
    with BudgetStore(path) as budgets:
        reservations = budgets.list_reservations("mission-1")
        assert len(reservations) == 1
        assert reservations[0].state is ReservationState.RESERVED

    replay = r'''
import pathlib, sys
from argus.governed_runtime import GovernedRuntime
from argus.effects import AmbiguousEffectError
from argus.effect_runtime import EffectExecutionOutcome, EffectExecutionResult
class Executor:
    def execute(self, context):
        pathlib.Path(sys.argv[2]).write_text('replayed')
        return EffectExecutionResult(
            correlation_key=context.effect.intent.correlation_key,
            attempt_key=context.attempt.attempt_key,
            outcome=EffectExecutionOutcome.APPLIED,
            evidence={'ref': 'opaque://unexpected'},
        )
try:
    GovernedRuntime(sys.argv[1]).execute_effect(
        'mission-1', 'step-0', 'effect-0', Executor(),
        now='2030-01-02T12:00:00Z', reserve_spend_usd='0.5'
    )
except AmbiguousEffectError:
    raise SystemExit(25)
'''
    replay_marker = tmp_path / "replay-effect.txt"
    blocked = _run_python(replay, path, replay_marker)
    assert blocked.returncode == 25
    assert not replay_marker.exists()
    with BudgetStore(path) as budgets:
        assert len(budgets.list_reservations("mission-1")) == 1

    class Reconciler:
        def reconcile(self, context):
            assert remote.read_text() == context.attempt.attempt_key
            return ReconciliationResult(
                correlation_key=context.effect.intent.correlation_key or "",
                attempt_key=context.attempt.attempt_key,
                decision=ReconciliationDecision.CONFIRMED_APPLIED,
                evidence={"remote_ref": "opaque://remote/applied"},
            )

    reconciled = EffectRuntime(path).reconcile_effect(
        "mission-1", "step-0", "effect-0", Reconciler(), now=_DUE
    )
    assert reconciled.decision is EffectRuntimeDecision.CONFIRMED_APPLIED
    with EffectAttemptStore(path) as attempts:
        assert len(attempts.list_attempts("mission-1", "step-0", "effect-0")) == 1

    # Cost settlement is explicit after trustworthy reconciliation evidence.
    with BudgetStore(path) as budgets:
        reservation = budgets.list_reservations("mission-1")[0]
        budgets.commit_spend(reservation.reservation_key, "0.3")
        assert budgets.load_reservation(reservation.reservation_key).state is ReservationState.COMMITTED


def test_governed_decisions_preserve_policy_provenance_across_restart(tmp_path) -> None:
    path = tmp_path / "argus.db"
    marker = tmp_path / "worker.txt"
    _prepare(path, worker_limit=1)
    with GuardrailStore(path) as guardrails:
        guardrails.pause("mission-1")
    denied = _run_python(_WORKER_CHILD, path, marker, "-")
    assert denied.returncode == 23

    events = GovernedRuntime(path).events("mission-1")
    assert len(events) == 1
    event = events[0]
    assert event.decision == "deny"
    assert event.reason == "mission_paused"
    assert event.guardrail_policy_hash.startswith("argus:guardrail:v1:")
    assert event.budget_policy_hash.startswith("argus:budget:v1:")


def test_phase4_fixture_values_remain_domain_opaque() -> None:
    values = (
        "mission-1 workload-alpha fixture.remote external.apply "
        "artifact://opaque/input opaque://remote/applied"
    )
    forbidden = ("youtube", "short", "retention", "editorial", "publication")
    for term in forbidden:
        assert term not in values.lower()
