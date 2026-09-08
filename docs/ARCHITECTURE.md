# ARGUS Architecture

## Purpose

ARGUS is the runtime and orchestration layer of Agentic Engineering Lab. Its job is to make long-running agent missions durable, inspectable, recoverable, policy-bound, and safe to resume.

ARGUS is not a domain application. Consumer applications keep their own business rules, APIs, schemas, quality policies, interpretation logic, and external-effect semantics.

## Architectural boundary

ARGUS owns generic execution concerns:

- mission and step lifecycle;
- durable state and audit history;
- future eligibility / scheduling;
- bounded worker execution;
- versioned worker input/output validation;
- retry, timeout, and failure classification;
- durable attempt evidence;
- idempotency and recovery coordination;
- usage/duration accounting;
- future generic side-effect, budget, and operator-control primitives.

Consumers own domain concerns. For the first reference workload:

```text
Digital Assets Lab                     ARGUS
------------------                     -----
YouTube metrics                        durable steps
Short performance analysis             due_at scheduling
editorial hypothesis                   bounded workers
video generation semantics             worker protocol validation
editorial QA                            retries / timeouts
publication policy                     attempt journal
YouTube upload/reconciliation          future generic effect protocol
```

ARGUS must never contain concepts such as `Short`, `YouTube`, `retention curve`, `editorial hook`, or `video publication policy`.

## Core control-plane rule

> The orchestrator is deterministic code. LLMs are bounded workers.

Prompt text is never the source of truth for mission lifecycle, retry policy, timing, recovery, budgets, or operator controls.

## Delivered architecture — Phase 1 + Phase 2

```text
versioned mission / opaque consumer payload
                  |
                  v
+------------------------------------+
| SQLite durable state               |
|------------------------------------|
| missions / ordered steps           |
| journal                            |
| scheduled_steps / schedule_events  |
| attempts / attempt_events          |
+------------------+-----------------+
                   |
         +---------+---------+
         |                   |
         v                   v
+----------------+   +-----------------------+
| Scheduler      |   | Phase2Runtime         |
| due_at / order |-->| due -> attempt ->     |
| read-only scan |   | bounded worker ->     |
+----------------+   | classify -> persist   |
                     +-----------+-----------+
                                 |
                                 v
                     +-----------------------+
                     | Worker boundary       |
                     | strict JSON v1        |
                     | bounded subprocess    |
                     | OpenCode adapter      |
                     +-----------------------+
```

### Mission and logical step model

The stable core states remain deliberately small:

```text
Mission: PENDING -> RUNNING -> COMPLETED | FAILED
Step:    PENDING -> RUNNING -> SUCCEEDED | FAILED
```

A versioned step envelope contains mission/step identity, ordinal, generic operation, opaque JSON payload, payload version, schema version, and deterministic idempotency key.

For retryable external-worker execution, the logical step stays `PENDING` while individual attempts are tracked separately. This avoids inventing an unsafe `RUNNING -> PENDING` reset.

On terminal worker success/failure, Phase 2 preserves the logical Phase 1 transition history atomically at the attempt-completion boundary.

### Persistence

SQLite is the local durable source of truth.

Phase 1 tables store mission/step definitions, terminal results, and append-only lifecycle history.

Phase 2 adds independently versioned extensions in the same SQLite state file:

- `scheduled_steps` — one active UTC `due_at` per scheduled logical step;
- `schedule_events` — schedule/reschedule/clear history;
- `attempts` — monotonic external invocation attempts;
- `attempt_events` — attempt lifecycle evidence.

The scheduler extensions do not rewrite Phase 1 envelopes. Malformed or unsupported persisted state fails closed.

### Scheduling

Waiting is currently represented as future eligibility rather than a new mission/step state.

A scheduled step is due only when:

- `due_at <= now`;
- its logical step is `PENDING`;
- its mission is non-terminal;
- all earlier ordered steps have succeeded.

Due scans are read-only. Repeating a scan creates no attempts and performs no worker invocation.

The current model is intentionally single-process. It does not claim distributed lease/claim semantics.

### Worker protocol and adapters

Worker invocation crosses a strict versioned JSON boundary:

```text
WorkerRequest v1
    -> bounded subprocess / adapter
        -> WorkerResponse v1
```

Responses declare one of:

- `success`;
- `retryable_failure`;
- `permanent_failure`.

Transport adds deterministic classifications such as timeout, process error, output-limit violation, malformed output, and termination uncertainty.

Worker output is untrusted until schema validation succeeds. Markdown wrappers, unknown fields, mismatched request IDs, non-finite JSON numbers, and malformed responses are rejected rather than heuristically repaired.

`OpenCodeWorkerAdapter` isolates current `opencode run` syntax from the runtime core. OpenCode is an execution backend, not an orchestration authority.

### Bounded subprocess execution

The transport provides:

- explicit timeout;
- bounded retained stdout/stderr;
- process-group termination on POSIX;
- fail-closed termination uncertainty where descendant termination cannot be established;
- closed stdin except when the generic JSON subprocess protocol intentionally supplies a request.

A timeout is not automatically equivalent to a safe retry. Retryability is a deterministic runtime policy and termination uncertainty always blocks automatic replay.

### Durable attempts and retries

Every Phase 2 external worker invocation is preceded by a committed attempt:

```text
scheduled step due
    -> attempt STARTED committed
        -> invoke worker
            -> attempt COMPLETED + classified outcome
```

If the runtime dies after `STARTED` but before a trustworthy outcome is committed, restart sees an ambiguous attempt and blocks replay.

Retry policy is deterministic and currently supports:

- maximum attempts;
- default retry delay;
- worker-declared retryable failure;
- optional validated `retry_after_seconds`;
- configurable process-error retryability;
- configurable proven-timeout retryability.

Permanent failure, malformed output, output-limit violations, and termination uncertainty are not blindly retried.

Attempts persist duration, exit code, optional token counts, optional worker-reported cost, retry due time, and bounded diagnostics. Raw payloads/stdout/stderr are not exposed by default inspection.

### Integrated Phase2Runtime

`Phase2Runtime` composes scheduling, durable attempts, and bounded workers into the local Phase 2 execution slice.

It adds two cross-cutting guarantees:

1. an injected logical `now` is used consistently for eligibility and deterministic retry timing;
2. if the final logical step is already durably `SUCCEEDED`, mission completion can be safely reconciled after restart without replaying a worker.

The safe mission-completion reconciliation is not external-effect reconciliation; it operates only on already-committed internal state.

### Operator evidence

The CLI exposes machine-readable evidence:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus schedule --store .argus/state.db --due-at <timestamp> <mission> <step>
argus due --store .argus/state.db --at <timestamp>
argus status --store .argus/state.db <mission>
argus inspect --store .argus/state.db <mission>
```

`status` includes due time, attempt count, and latest attempt state/outcome. `inspect` includes lifecycle journal, schedule history, and durable attempt metadata without raw consumer payloads.

## Restart guarantees

Phase 1 proves that a durably completed logical step is not re-executed after process restart.

Phase 2 extends the failure model:

- future `due_at` remains discoverable after restart;
- a committed `STARTED` attempt with no trustworthy result blocks blind replay;
- process-tree termination uncertainty blocks blind replay;
- a retryable completed attempt can schedule a future deterministic retry;
- a crash after final step commit but before mission completion can be reconciled safely from durable internal state.

See [Restart and Recovery](RECOVERY.md).

## Phase 3 target — side-effect safety

Phase 2 deliberately does **not** claim exactly-once external effects.

The next generic protocol will be driven by the DAL publication/integration need and is expected to introduce some form of:

```text
persist external-effect intent
    -> invoke effect
        -> persist receipt/outcome
            -> reconcile ambiguity before any duplicate attempt
```

The consumer remains responsible for domain-specific reconciliation logic; ARGUS supplies only the generic durable protocol and execution gates.

## Later operational controls

Future phases may add, only when exercised:

- mission/attempt/spend budgets;
- pause/resume/cancel;
- workload/global kill switches;
- policy versioning and structured audit decisions;
- always-on remote service mode;
- backup/restore and health reporting;
- eventually, concurrency or distributed execution if a real workload requires it.

Budgets and kill switches must be deterministic runtime controls, never instructions that depend on model cooperation.

## Human-over-the-loop

ARGUS is designed so humans define policy and supervise missions rather than approve every routine step.

Human intervention should be reserved for genuine exceptions such as interactive credentials, unresolved policy ambiguity, exhausted budgets, or external effects that cannot be reconciled safely.

## Architecture rule for future contributions

Before adding a capability to ARGUS, ask:

> Would this capability still make sense if the first consumer were not Digital Assets Lab?

If the answer is no, it probably belongs in the consumer application.
