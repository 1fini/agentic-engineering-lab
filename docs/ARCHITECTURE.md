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
- durable computational attempt evidence;
- generic external-effect intent / receipt / reconciliation lifecycle;
- durable effect-attempt lineage and duplicate-replay prevention;
- usage/duration accounting;
- future generic budget and operator-control primitives.

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
YouTube upload                          generic effect intent/attempt gate
YouTube remote lookup                   generic reconciliation interface
publication identity semantics         opaque effect correlation/receipt data
```

ARGUS must never contain concepts such as `Short`, `YouTube`, `retention curve`, `editorial hook`, or `video publication policy`.

## Core control-plane rule

> The orchestrator is deterministic code. LLMs are bounded workers.

Prompt text is never the source of truth for mission lifecycle, retry policy, timing, recovery, side-effect replay, budgets, or operator controls.

## Delivered architecture — Phase 1 + Phase 2 + Phase 3

```text
versioned mission / opaque consumer payload
                  |
                  v
+--------------------------------------------------+
| SQLite durable state                             |
|--------------------------------------------------|
| missions / ordered steps / journal               |
| scheduled_steps / schedule_events                |
| attempts / attempt_events                        |
| effects / effect_events / effect_attempts         |
+-------------------------+------------------------+
                          |
            +-------------+-------------+
            |                           |
            v                           v
+--------------------+       +----------------------------+
| Phase2Runtime      |       | EffectRuntime              |
| due -> attempt ->  |       | intent -> attempt ->       |
| worker -> classify |       | external call -> receipt   |
| -> persist/retry   |       | or reconcile ambiguity    |
+---------+----------+       +-------------+--------------+
          |                                |
          v                                v
+--------------------+       +----------------------------+
| Worker boundary    |       | Consumer effect boundary   |
| strict JSON v1     |       | EffectExecutor             |
| bounded subprocess |       | EffectReconciler           |
| OpenCode adapter   |       | opaque remote semantics    |
+--------------------+       +----------------------------+
```

## Mission and logical step model

The stable core states remain deliberately small:

```text
Mission: PENDING -> RUNNING -> COMPLETED | FAILED
Step:    PENDING -> RUNNING -> SUCCEEDED | FAILED
```

Worker retries and external-effect attempts are tracked outside the logical step state so a retry never requires an unsafe `RUNNING -> PENDING` reset.

## Persistence

SQLite is the local durable source of truth.

Phase 1 stores mission/step definitions, terminal results, and append-only lifecycle history.

Phase 2 adds independently versioned scheduling and computational attempt extensions.

Phase 3 adds independently versioned effect state and lineage:

- `effects` — one durable parent effect identity and current lifecycle state;
- `effect_events` — append-only effect lifecycle history;
- `effect_attempts` — monotonic external-effect attempt identity/history, distinct from worker attempts.

Malformed or unsupported persisted state fails closed.

## Scheduling and bounded workers

A scheduled step is due only when its `due_at` has passed, the logical step is still pending, the mission is non-terminal, and ordered dependencies have succeeded.

Due scans are read-only. Every external worker invocation is preceded by a durable Phase 2 attempt. An ambiguous `STARTED` attempt blocks replay. OpenCode remains an adapter behind the generic worker boundary.

## Phase 3 external-effect protocol

Phase 3 separates computational retry from non-idempotent external effects.

The parent effect lifecycle is:

```text
INTENT_COMMITTED
      |
      | begin external attempt
      v
OUTCOME_UNKNOWN
      |
      +--> execution receipt APPLIED --------> CONFIRMED_APPLIED
      |
      +--> execution receipt NOT_APPLIED ----> CONFIRMED_NOT_APPLIED
      |
      +--> no trustworthy receipt
                |
                v
          reconciliation
          /      |       \
   APPLIED  NOT_APPLIED  UNKNOWN
      |          |          |
      v          v          v
 CONFIRMED   CONFIRMED    BLOCKED
  APPLIED    NOT_APPLIED
```

### Persist intent before effect

An `EffectIntent` is durable before any consumer executor may be called. The effect has a stable parent correlation key derived from canonical durable intent.

### Enter ambiguity before leaving the local boundary

Before external consumer code is invoked, ARGUS durably records `OUTCOME_UNKNOWN` and allocates the current effect attempt identity. Therefore a process death after the remote call begins never looks like an ordinary retryable failure.

### Consumer-owned reconciliation

ARGUS owns only the generic interface and typed decision. The consumer decides how to inspect its remote system and returns one of:

- `CONFIRMED_APPLIED`;
- `CONFIRMED_NOT_APPLIED`;
- `STILL_UNKNOWN`.

ARGUS never infers remote state from prose or platform heuristics.

### Replay policy

- `CONFIRMED_APPLIED` permanently forbids another external call for that effect lineage.
- `STILL_UNKNOWN` remains blocked and fail-closed.
- `CONFIRMED_NOT_APPLIED` is the only state that may authorize another effect attempt.
- A new attempt gets a monotonic versioned attempt identity while preserving the stable parent effect correlation.
- Duplicate restart/polling cannot allocate two active effect attempts in the current local single-process model.

Phase 2 worker retry classification alone can never authorize an effect replay.

## Crash-boundary evidence

The Phase 3 acceptance suite uses real subprocess termination and a fake external system persisted separately from ARGUS SQLite. It proves:

- crash after intent but before external call can resume safely;
- crash after remote acceptance but before local receipt produces explicit ambiguity;
- restart performs no blind replay;
- reconciliation APPLIED advances without a duplicate external call;
- reconciliation NOT_APPLIED may authorize exactly one policy-bounded re-attempt;
- reconciliation UNKNOWN remains blocked across repeated restarts;
- crash after authoritative receipt but before auxiliary lineage synchronization is repaired from durable local evidence without re-executing the effect.

ARGUS does not claim exactly-once external effects for arbitrary remote systems. It guarantees **examined replay**: ambiguity must be resolved before another call can be authorized.

## Operator evidence

`argus status` and `argus inspect` expose machine-readable evidence for mission/step state, schedules, worker attempts, effect identities, receipt outcomes/sources, effect attempts, and correlation lineage. Raw consumer payloads and receipt evidence are not emitted by default.

## Next architecture target — Phase 4 operational guardrails

The next generic layer, driven by DAL unattended operation, will gate work on durable operator/policy state:

```text
candidate execution
      |
      v
policy / budget / operator gate
      |
  allow or deny
      |
      +--> worker attempt
      +--> effect attempt
```

Expected primitives include mission/attempt/spend budgets, durable reservations where necessary, pause/resume/cancel, workload/global kill switch, policy versioning, and structured audit decisions.

Budgets and kill switches must be deterministic runtime controls, never instructions that depend on model cooperation.

## Human-over-the-loop

ARGUS is designed so humans define policy and supervise missions rather than approve every routine step. Human intervention is reserved for genuine exceptions such as interactive credentials, unresolved policy ambiguity, exhausted budgets, or external effects that cannot be reconciled safely.

## Architecture rule for future contributions

Before adding a capability to ARGUS, ask:

> Would this capability still make sense if the first consumer were not Digital Assets Lab?

If the answer is no, it probably belongs in the consumer application.
