# ARGUS Architecture

## Purpose

ARGUS is the runtime and orchestration layer of Agentic Engineering Lab.

Its job is to make long-running agent missions durable, inspectable, recoverable, policy-bound, and safe to resume. It is not a domain application and it must not absorb business logic from its consumers.

## Architectural boundary

ARGUS owns generic execution concerns:

- mission and step lifecycle;
- durable state;
- scheduling and wakeups;
- bounded worker execution;
- structured input/output validation;
- retry, timeout, and error classification;
- idempotency and recovery coordination;
- correlated logs, events, artifacts, and evidence;
- budgets and policy checks;
- pause, resume, cancellation, and kill switch.

Consumer applications own domain concerns:

- business rules;
- domain-specific state transitions;
- domain APIs and semantics;
- interpretation of domain metrics;
- generation rules;
- publication or transaction rules;
- domain-specific quality and policy checks.

A concrete example:

```text
Digital Assets Lab                     ARGUS
------------------                     -----
YouTube metrics                        durable step execution
Short performance analysis             scheduler / wakeups
editorial hypothesis                   worker invocation
video generation semantics             retries / timeouts
editorial QA                            state persistence
publication policy                     budgets / kill switch
YouTube upload reconciliation          generic side-effect protocol support
```

ARGUS must never contain concepts such as `Short`, `YouTube`, `retention curve`, `editorial hook`, or `video publication policy`.

## Core control-plane rule

> The orchestrator is deterministic code. LLMs are bounded workers.

ARGUS owns control flow and durable state transitions. Workers are external computations invoked behind explicit contracts. Prompt text is never the source of truth for mission lifecycle, retries, budgets, or recovery.

## Implemented architecture — Phase 1

Mission #1 implements the first durable local slice.

```text
versioned JSON mission manifest
             |
             v
+---------------------------+
| Deterministic runner      |
| - persisted ordinal order |
| - explicit worker registry|
| - no prompt control flow  |
+-------------+-------------+
              |
              v
+---------------------------+
| SQLite durable store      |
| - missions                |
| - ordered steps           |
| - terminal results        |
| - append-only journal     |
+-------------+-------------+
              |
              v
+---------------------------+
| CLI / evidence            |
| run / status / inspect    |
| compact JSON              |
+---------------------------+
```

The Phase 1 runtime uses Python 3.12, standard-library SQLite, direct SQL, `argparse`, and pytest as recorded by ADR-0001.

### Current mission model

Phase 1 mission states are intentionally smaller than the long-term target:

```text
PENDING -> RUNNING -> COMPLETED
    |         |
    +-------> FAILED
```

`COMPLETED` and `FAILED` are terminal. A mission may complete only after every durable step is `SUCCEEDED`.

Future `WAITING`, `PAUSED`, `BLOCKED`, and `CANCELLED` lifecycle states are not yet persisted as mission states; Phase 1 can instead return a typed runtime block when safe progress is impossible.

### Current step model

A Phase 1 step envelope v1 contains:

- `mission_id`;
- `step_id`;
- zero-based `ordinal`;
- generic `operation`;
- opaque JSON `payload`;
- `payload_version`;
- envelope schema version;
- deterministic idempotency key.

Step states are:

```text
PENDING -> RUNNING -> SUCCEEDED
    |         |
    +-------> FAILED
```

A terminal result is typed `success` or `failure` and contains opaque JSON output. Consumer payload fields are not interpreted by ARGUS.

The deterministic idempotency key is derived from canonical JSON of the versioned step definition. It protects identity of a durable step definition; it is not a claim of exactly-once external side effects.

### Current persistence contract

SQLite stores:

- storage schema metadata;
- durable mission state;
- ordered versioned step envelopes;
- terminal typed step results;
- append-only creation/state-transition journal.

A state transition and its journal entry commit in the same SQLite transaction. Read-time validation fails closed for unsupported versions, malformed persisted JSON, invalid states, inconsistent terminal results, non-contiguous ordinals, mismatched idempotency keys, or invalid SQLite state.

See `docs/DURABLE_STATE.md`.

### Current execution contract

The Phase 1 runner:

1. reads persisted state;
2. selects the next eligible step deterministically by ordinal;
3. resolves its operation through an explicit callable registry;
4. commits `RUNNING` before invoking the worker;
5. persists a valid success/failure result before advancing;
6. skips already-succeeded steps;
7. treats a completed mission rerun as a no-op.

An unregistered operation blocks before the step enters `RUNNING`.

An explicit deterministic worker failure becomes a durable failed result. An unexpected exception leaves the step `RUNNING` rather than inventing a result. Real `KeyboardInterrupt` / `SystemExit` are not swallowed.

See `docs/RUNNER.md`.

### Current restart guarantee

Phase 1 proves two separate crash boundaries with real subprocess termination:

**Crash after a terminal step commit:** the committed `SUCCEEDED` record is authoritative. Restart skips that step and executes only later pending steps. The acceptance test verifies an external execution log contains each step exactly once.

**Crash while a step is `RUNNING`:** ARGUS cannot establish the worker's outcome. Restart blocks and does not blindly replay the step.

Therefore the Phase 1 guarantee is:

> durably completed steps are not re-executed after restart.

It is deliberately **not** a guarantee of exactly-once external effects for work left `RUNNING`.

See `docs/RECOVERY.md`.

### Current operator surface

The current local control surface is machine-readable:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db <mission-id>
argus inspect --store .argus/state.db <mission-id>
```

The fixture registry exists only for executable Phase 1 tests/examples. A production worker backend is not yet implemented.

## Target worker execution — not implemented yet

The first production worker backend is expected to integrate with OpenCode and cloud-hosted models while remaining backend-agnostic at the runtime boundary.

A later worker adapter should support at least:

- explicit agent/model configuration;
- structured input;
- output schema validation;
- process/request timeout;
- process-tree termination;
- retryable/permanent failure classification;
- duration and cost/token accounting when available.

Malformed worker output must be a typed failure, never an invitation to infer intent from prose.

## Target scheduling — not implemented yet

Long-running missions need durable future wakeups instead of process-local sleeps.

A later phase will introduce persisted waiting semantics such as `due_at`, a local scheduler loop, and restart discovery of due work. This must build on the existing durable state boundary rather than create a parallel scheduler state store.

## Target side-effect safety — not implemented yet

ARGUS must not blindly retry non-idempotent external operations.

The target protocol is:

```text
persist intent
    -> execute external call
        -> persist outcome
            -> verify / reconcile if necessary
```

If an external effect may have happened but no local result was committed, a future Phase 3 protocol will represent that ambiguity explicitly and invoke a consumer-owned reconciliation mechanism before permitting another side effect.

Phase 1's fail-closed handling of `RUNNING` after process death is the conservative precursor to this protocol.

## Target observability and controls — not implemented yet

Later phases should extend the persisted audit model with, when available:

- attempts;
- input/output digests;
- worker/model identity;
- duration and cost;
- artifact references;
- policy decisions;
- correlation IDs;
- due times;
- budget reservations and exhaustion;
- pause/resume/cancel/kill-switch state.

Budgets and kill switches must be deterministic runtime controls, not instructions that depend on model cooperation.

## Human-over-the-loop

ARGUS is designed for systems where humans set policy and supervise missions rather than approve routine steps.

Human intervention should be reserved for genuine exceptions such as interactive credentials, unresolved policy ambiguity, exhausted configured budgets, or external effects that cannot be reconciled under current policy.

Phase 1 does not yet implement those operational states, but it establishes the durability and fail-closed semantics they require.

## Deployment direction

Current implementation:

```text
local machine
  -> ARGUS single-process runtime
  -> local SQLite durable state
  -> explicit Python fixture/callable workers
```

Later target:

```text
operator CLI
    |
    v
remote always-on ARGUS runtime
    |
    +--> bounded worker backends / OpenCode
    +--> external systems
    +--> durable state
```

The CLI remains a control surface. Mission durability belongs in the runtime/state store, not in an interactive terminal session.

## Architecture rule for future contributions

Before adding a capability to ARGUS, ask:

> Would this capability still make sense if the first consumer were not Digital Assets Lab?

If the answer is no, it probably belongs in the consumer application.
