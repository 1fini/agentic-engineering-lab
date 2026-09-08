from __future__ import annotations

import sqlite3

import pytest

from argus.guardrails import (
    GateDecision,
    GateReason,
    GuardrailError,
    GuardrailPolicy,
    GuardrailStore,
    MissionControlState,
)
from argus.model import StepEnvelope
from argus.store import SqliteMissionStore


def _mission(path, mission_id="mission-1") -> None:
    with SqliteMissionStore(path) as store:
        store.create_mission(
            mission_id,
            [
                StepEnvelope(
                    mission_id=mission_id,
                    step_id="step-0",
                    ordinal=0,
                    operation="fixture.compute",
                    payload={"input_ref": "artifact://opaque"},
                )
            ],
        )


def _policy(mission_id="mission-1", *, version=1, ref="policy://v1") -> GuardrailPolicy:
    return GuardrailPolicy(
        mission_id=mission_id,
        workload_scope="workload-alpha",
        policy_version=version,
        policy_ref=ref,
    )


def test_policy_is_durable_idempotent_and_restart_visible(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    policy = _policy()

    with GuardrailStore(path) as guardrails:
        first = guardrails.set_policy(policy)
        second = guardrails.set_policy(policy)
        assert first == second
        assert first.control_state is MissionControlState.ACTIVE
        assert first.policy.policy_hash.startswith("argus:guardrail:v1:")
        assert [event.event_type for event in guardrails.history("mission-1")] == [
            "policy_created"
        ]

    with GuardrailStore(path) as reopened:
        durable = reopened.load("mission-1")
        assert durable.policy == policy
        assert reopened.check_gate("mission-1").decision is GateDecision.ALLOW


def test_policy_updates_require_monotonic_version_and_immutable_scope(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())
        updated = guardrails.set_policy(_policy(version=2, ref="policy://v2"))
        assert updated.policy.policy_version == 2
        with pytest.raises(GuardrailError, match="strictly increasing"):
            guardrails.set_policy(_policy(version=1, ref="policy://different"))
        with pytest.raises(GuardrailError, match="workload_scope is immutable"):
            guardrails.set_policy(
                GuardrailPolicy(
                    mission_id="mission-1",
                    workload_scope="other-scope",
                    policy_version=3,
                )
            )


def test_pause_resume_and_cancel_survive_restart(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())
        paused = guardrails.pause("mission-1")
        assert paused.control_state is MissionControlState.PAUSED
        assert guardrails.check_gate("mission-1").reason is GateReason.MISSION_PAUSED

    with GuardrailStore(path) as reopened:
        assert reopened.load("mission-1").control_state is MissionControlState.PAUSED
        resumed = reopened.resume("mission-1")
        assert resumed.control_state is MissionControlState.ACTIVE
        cancelled = reopened.cancel("mission-1")
        assert cancelled.control_state is MissionControlState.CANCELLED

    with GuardrailStore(path) as reopened:
        assert reopened.check_gate("mission-1").reason is GateReason.MISSION_CANCELLED
        with pytest.raises(GuardrailError, match="cannot be resumed"):
            reopened.resume("mission-1")
        with pytest.raises(GuardrailError, match="terminal"):
            reopened.pause("mission-1")


def test_gate_precedence_cancel_global_workload_pause_allow(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path, "cancelled")
    _mission(path, "paused")
    _mission(path, "active")
    with GuardrailStore(path) as guardrails:
        for mission_id in ("cancelled", "paused", "active"):
            guardrails.set_policy(_policy(mission_id))
        guardrails.cancel("cancelled")
        guardrails.pause("paused")
        guardrails.set_workload_kill("workload-alpha", True)
        guardrails.set_global_kill(True)

        assert guardrails.check_gate("cancelled").reason is GateReason.MISSION_CANCELLED
        assert guardrails.check_gate("paused").reason is GateReason.GLOBAL_KILL
        assert guardrails.check_gate("active").reason is GateReason.GLOBAL_KILL

        guardrails.set_global_kill(False)
        assert guardrails.check_gate("paused").reason is GateReason.WORKLOAD_KILL
        assert guardrails.check_gate("active").reason is GateReason.WORKLOAD_KILL

        guardrails.set_workload_kill("workload-alpha", False)
        assert guardrails.check_gate("paused").reason is GateReason.MISSION_PAUSED
        allowed = guardrails.check_gate("active")
        assert allowed.reason is GateReason.ALLOWED
        assert allowed.decision is GateDecision.ALLOW


def test_gate_checks_are_read_only_while_audit_is_explicit(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())
        baseline = len(guardrails.history())
        first = guardrails.check_gate("mission-1")
        second = guardrails.check_gate("mission-1")
        assert first == second
        assert len(guardrails.history()) == baseline

        audited = guardrails.audit_gate("mission-1")
        assert audited == first
        events = guardrails.history()
        assert len(events) == baseline + 1
        assert events[-1].event_type == "gate_decision"
        assert events[-1].decision == "allow"
        assert events[-1].reason == "allowed"
        assert events[-1].policy_hash == first.policy_hash


def test_kill_switches_are_durable_and_idempotent(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())
        assert guardrails.set_workload_kill("workload-alpha", True) is True
        event_count = len(guardrails.history())
        assert guardrails.set_workload_kill("workload-alpha", True) is True
        assert len(guardrails.history()) == event_count
        guardrails.set_global_kill(True)

    with GuardrailStore(path) as reopened:
        assert reopened.workload_kill_enabled("workload-alpha") is True
        assert reopened.global_kill_enabled() is True
        assert reopened.check_gate("mission-1").reason is GateReason.GLOBAL_KILL


def test_guardrails_require_existing_mission_store_and_mission(tmp_path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(GuardrailError, match="initialized ARGUS mission store"):
        GuardrailStore(path)

    initialized = tmp_path / "argus.db"
    _mission(initialized)
    with GuardrailStore(initialized) as guardrails:
        with pytest.raises(KeyError, match="unknown mission"):
            guardrails.set_policy(_policy("unknown"))


def test_corrupt_policy_hash_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE mission_guardrails SET policy_hash = 'corrupt' WHERE mission_id = 'mission-1'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(GuardrailError, match="policy hash mismatch"):
        GuardrailStore(path)


def test_invalid_guardrail_schema_version_fails_closed(tmp_path) -> None:
    path = tmp_path / "argus.db"
    _mission(path)
    with GuardrailStore(path) as guardrails:
        guardrails.set_policy(_policy())

    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE argus_guardrail_metadata SET value = '99' WHERE key = 'schema_version'"
    )
    connection.commit()
    connection.close()

    with pytest.raises(GuardrailError, match="unsupported guardrail schema version"):
        GuardrailStore(path)


def test_fixture_terms_remain_domain_opaque() -> None:
    source = __file__
    text = open(source, encoding="utf-8").read().lower()
    forbidden = ("youtube", "short", "retention", "editorial", "publication")
    for term in forbidden:
        assert term not in text
