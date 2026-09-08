# ARGUS Guardrail Controls

Phase 4 introduces deterministic operator controls in front of autonomous execution. These controls are runtime state, not prompt instructions.

## Scope of WS1

The first Phase 4 workstream delivers control state and kill switches only. Attempt/spend budgets and integration into worker/effect execution are separate workstreams.

## Versioned policy identity

A mission may attach a generic `GuardrailPolicy` containing:

- mission identity;
- workload scope;
- monotonic policy version;
- optional opaque policy reference;
- deterministic policy hash.

The workload scope is immutable after initialization. A policy update requires a strictly larger version. Raw policy references are not emitted by normal `status` or `inspect` output.

## Mission control states

```text
ACTIVE <-> PAUSED
   |
   +------> CANCELLED

CANCELLED -> terminal
```

Repeated requests for the already-current state are idempotent where safe. A cancelled mission cannot be resumed or paused.

## Kill switches

ARGUS persists two kill-switch scopes:

- **global** — applies to every governed workload;
- **workload** — applies to one opaque workload scope.

Kill switches survive runtime restart. Enabling/disabling the same switch to its current value is idempotent and does not append duplicate transition events.

## Deterministic gate precedence

WS1 evaluates controls in this order:

1. mission cancelled;
2. global kill switch;
3. workload kill switch;
4. mission paused;
5. allow.

This precedence is deterministic code. Workers and models cannot override it.

`check_gate()` is read-only: repeated checks do not mutate state or consume future budgets. `audit_gate()` evaluates the same decision and appends an explicit decision event with policy version/hash provenance.

## Audit evidence

The guardrail journal records:

- policy creation/update;
- pause/resume/cancel;
- global/workload kill changes;
- explicit audited gate decisions.

`status` exposes current control state and gate result. `inspect` exposes the relevant append-only control history without consumer payloads.

## CLI

```bash
argus guardrail-init --store .argus/state.db \
  --workload-scope <scope> --policy-version 1 <mission-id>

argus control --store .argus/state.db pause <mission-id>
argus control --store .argus/state.db resume <mission-id>
argus control --store .argus/state.db cancel <mission-id>
argus control --store .argus/state.db gate <mission-id>

argus kill-switch --store .argus/state.db --global-scope on
argus kill-switch --store .argus/state.db --global-scope off
argus kill-switch --store .argus/state.db --workload-scope <scope> on
argus kill-switch --store .argus/state.db --workload-scope <scope> off
```

## What WS1 does not yet guarantee

The control store does not yet sit on the Phase 2/3 invocation paths. That integration is owned by later Mission #30 workstreams. Until then the durable gate is executable and inspectable, but the complete Phase 4 Definition of Done is not satisfied.

Budgets, spend reservations, worker/effect gate composition, and real crash-boundary acceptance remain later workstreams under Mission #30.
