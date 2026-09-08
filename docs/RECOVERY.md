# ARGUS Phase 1 Restart and Recovery Semantics

Mission #1 workstream #5 proves the durability boundary with real subprocess termination.

## Committed terminal step

If a process dies **after** a step's `SUCCEEDED` result and journal transition are committed, that durable state is authoritative.

On restart ARGUS:

1. opens the same SQLite store;
2. validates the stored schema/state;
3. sees the step as `SUCCEEDED`;
4. skips it;
5. continues with the next pending ordinal.

The committed step is not executed again.

The acceptance suite proves this by appending each worker invocation to a separate execution log, terminating the ARGUS subprocess immediately after step 0's success commit, restarting, and asserting the final log contains exactly one invocation of each step.

## RUNNING step after process death

A process may also die after ARGUS commits `PENDING -> RUNNING` but before a durable result exists.

Phase 1 deliberately treats this as an **ambiguous execution boundary**. ARGUS does not know whether the worker performed an external effect before dying.

Therefore restart behavior is fail-closed:

```text
RUNNING after restart -> BLOCKED
```

ARGUS does **not** automatically reset the step to pending and does **not** invoke the worker again.

A dedicated subprocess test kills the worker while the step is `RUNNING`, then proves a restarted runner blocks without a second invocation.

This is not yet full side-effect reconciliation. A later ARGUS mission will introduce explicit intent/receipt/reconciliation contracts for external effects.

## Evidence

`argus inspect` is the Phase 1 machine-readable evidence bundle. It exposes:

- mission state;
- ordered step identity, operation, state and deterministic idempotency key;
- terminal result kind;
- append-only creation/state-transition journal with sequence and timestamps.

Raw consumer payloads are not printed by default.

## Idempotent duplicate run

Running an already-completed mission is a no-op. No worker is invoked and no duplicate journal transitions are appended.

## Corruption

The durable-state validation implemented in workstream #3 still applies on restart. Invalid SQLite files, unsupported schema versions, malformed payload/result data, inconsistent persisted states, and mismatched idempotency data fail explicitly rather than being repaired heuristically.

## Phase 1 guarantee

Phase 1 guarantees exactly-once **re-execution avoidance for durably completed steps** within the local single-process model.

It does not claim exactly-once external side effects for a step that died while `RUNNING`. Those remain blocked until a later reconciliation protocol can establish the external outcome.
