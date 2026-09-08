# ARGUS Architecture

## Purpose

ARGUS is the runtime and orchestration layer of Agentic Engineering Lab. Its job is to make long-running agent missions durable, inspectable, recoverable, policy-bound, governable, and safe to resume.

ARGUS is not a domain application. Consumer applications keep their own business rules, APIs, schemas, quality policies, interpretation logic, external-effect semantics, and business-specific risk thresholds.

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
- durable operator control state;
- workload/global kill switches;
- generic attempt and spend budgets;
- crash-safe spend reservations;
- deterministic governed execution gates and policy provenance.

Consumers own domain concerns. For the first reference workload:

```text
Digital Assets Lab                     ARGUS
------------------                     -----
YouTube metrics                        durable steps
Short performance analysis             due_at scheduling
editorial hypothesis                   bounded workers
video generation semantics             worker protocol validation
editorial QA                            retries / timeouts
publication policy                     computational attempt journal
YouTube upload                          generic effect intent/attempt gate
YouTube remote lookup                   generic reconciliation interface
publication identity semantics         opaque effect correlation/receipt data
business risk thresholds               generic guardrail/budget values
```

ARGUS must never contain concepts such as `Short`, `YouTube`, `retention curve`, `editorial hook`, or `video publication policy`.

## Core control-plane rule

> The orchestrator is deterministic code. LLMs are bounded workers.

Prompt text is never the source of truth for mission lifecycle, retry policy, timing, recovery, side-effect replay, budgets, or operator controls.

## Delivered architecture — Phase 1 through Phase 4

```text
versioned mission / opaque consumer payload
                  |
                  v
+---------------------------------------------------------------+
| SQLite durable state                                          |
|---------------------------------------------------------------|
| missions / ordered steps / journal                            |
| scheduled_steps / schedule_events                             |
| attempts / attempt_events                                     |
| effects / effect_events / effect_attempts                     |
| mission_guardrails / kill_switches / guardrail_events         |
| budget_policies / spend_reservations / budget_events          |
| governed_execution_events                                     |
+-----------------------------+---------------------------------+
                              |
                              v
                   +-----------------------+
                   | GovernedRuntime       |
                   |-----------------------|
                   | control / kill gate   |
                   | attempt budget gate   |
                   | spend reservation     |
                   | policy provenance     |
                   +-----+-----------+-----+
                         |           |
              +----------+           +-----------+
              v                                  v
+---------------------------+       +----------------------------+
| Phase2Runtime             |       | EffectRuntime              |
| due -> attempt -> worker  |       | intent -> attempt ->       |
| -> classify -> retry      |       | external call -> receipt   |
+-------------+-------------+       | or reconcile ambiguity     |
              |                     +-------------+--------------+
              v                                   |
+---------------------------+                     v
| Worker boundary           |       +----------------------------+
| strict JSON v1            |       | Consumer effect boundary   |
| bounded subprocess        |       | EffectExecutor/Reconciler  |
| OpenCode adapter          |       | opaque remote semantics    |
+---------------------------+       +----------------------------+
```

## Mission and logical step model

The stable core states remain deliberately small:

```text
Mission: PENDING -> RUNNING -> COMPLETED | FAILED
Step:    PENDING -> RUNNING -> SUCCEEDED | FAILED
```

Worker retries and external-effect attempts are tracked outside the logical step state so a retry never requires an unsafe `RUNNING -> PENDING` reset.

Phase 4 operator control is also separate from mission lifecycle:

```text
Control: ACTIVE <-> PAUSED -> CANCELLED
```

`CANCELLED` is terminal. A paused mission keeps its durable mission/step state unchanged and may later resume without replaying completed work.

## Persistence

SQLite is the local durable source of truth.

Phase 1 stores mission/step definitions, terminal results, and append-only lifecycle history.

Phase 2 adds independently versioned scheduling and computational attempt extensions.

Phase 3 adds independently versioned effect state and lineage:

- `effects` — one durable parent effect identity and current lifecycle state;
- `effect_events` — append-only effect lifecycle history;
- `effect_attempts` — monotonic external-effect attempt identity/history, distinct from worker attempts.

Phase 4 adds independently versioned governance state:

- `mission_guardrails` — versioned mission control policy and ACTIVE/PAUSED/CANCELLED state;
- `kill_switches` — durable global and workload-scoped kill state;
- `guardrail_events` — append-only policy/control/kill/gate evidence;
- `budget_policies` — worker-attempt, effect-attempt, and optional spend limits;
- `spend_reservations` — crash-safe reservation ledger;
- `budget_events` — append-only budget/reservation evidence;
- `governed_execution_events` — allow/deny decisions with both policy hashes and reservation identity.

Malformed or unsupported persisted state fails closed.

## Scheduling and bounded workers

A scheduled step is due only when its `due_at` has passed, the logical step is still pending, the mission is non-terminal, and ordered dependencies have succeeded.

Due scans are read-only. Every external worker invocation is preceded by a durable Phase 2 attempt. An ambiguous `STARTED` attempt blocks replay. OpenCode remains an adapter behind the generic worker boundary.

## Phase 3 external-effect protocol

Phase 3 separates computational retry from non-idempotent external effects.

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

An `EffectIntent` is durable before consumer executor code. Before the external call, ARGUS durably records `OUTCOME_UNKNOWN` and allocates the effect attempt identity. A process death after that boundary therefore cannot become an ordinary retry.

Reconciliation remains consumer-owned behind typed generic contracts. Confirmed applied permanently forbids replay; confirmed not applied is the only state that may authorize a bounded new attempt; unknown remains blocked.

## Phase 4 operator controls

Mission control and kill switches are deterministic runtime state, not prompt instructions.

The control gate precedence is:

```text
CANCELLED?      -> DENY
GLOBAL KILL?    -> DENY
WORKLOAD KILL?  -> DENY
PAUSED?         -> DENY
otherwise       -> continue to budget gate
```

A plain gate check is read-only. Explicit governed execution records an auditable allow/deny decision with policy version/hash provenance.

## Phase 4 budgets

`BudgetPolicy` supports independent limits for:

- worker attempts;
- effect attempts;
- optional observable spend/cost.

Attempt consumption is derived from the authoritative Phase 2 and Phase 3 attempt tables, so a crash cannot erase already-allocated work by desynchronizing a duplicate counter.

Spend uses a durable ledger:

```text
RESERVED -> COMMITTED
         -> RELEASED
```

Outstanding reservations reduce available capacity after restart. ARGUS never automatically releases an ambiguous reservation.

When a spend budget is configured, governed cost-bearing execution requires an explicit reservation upper bound before invocation. Trusted actual worker cost may commit less than the reservation. Effect cost remains consumer-resolved through a generic trusted result resolver; if cost cannot be proven, the reservation stays outstanding.

## Governed execution boundary

`GovernedRuntime` is the authoritative surface for continuous unattended Phase 4 execution.

For a worker:

```text
due + no Phase2 ambiguity
        -> control/kill gate
        -> worker-attempt budget gate
        -> durable spend reservation (when configured)
        -> Phase2 begin_attempt
        -> worker invocation
        -> durable outcome / retry
        -> spend settlement when trustworthy
```

For an external effect:

```text
valid Phase3 effect state
        -> control/kill gate
        -> effect-attempt budget gate
        -> durable spend reservation (when configured)
        -> Phase3 effect-attempt allocation / OUTCOME_UNKNOWN
        -> external call
        -> receipt or reconciliation
        -> spend settlement when trustworthy
```

A confirmed-not-applied effect re-attempt must pass the Phase 4 gate again and consumes the next effect-attempt budget slot.

### Compatibility boundary

`Phase2Runtime` and `EffectRuntime` remain lower-level recovery primitives for tests, migrations, and explicit composition. They predate Phase 4 and are intentionally not silently changed to require governance policies.

A consumer that intends to run continuously without human approval must enter worker/effect execution through `GovernedRuntime`; calling lower-level primitives directly is not the Phase 4 unattended contract.

## Reservation crash semantics

The deterministic reservation key is derived from mission, execution kind, subject, and next authoritative attempt number.

A critical Phase 4 acceptance case is:

```text
ALLOW
  -> spend reservation committed
  -> process dies before attempt allocation
  -> restart
  -> same next attempt identity
  -> same reservation key
  -> reuse existing reservation idempotently
  -> continue without a second reservation
```

The acceptance suite initially exposed a bug where restart checked fresh remaining capacity before recognizing the existing reservation. That would self-deny as `spend_exhausted`. The delivered implementation resolves/reuses the deterministic reservation before any fresh-capacity decision.

If the process instead dies after a worker `STARTED` attempt or Phase 3 `OUTCOME_UNKNOWN` boundary, recovery ambiguity is detected before allocating another reservation. The existing reservation remains outstanding while replay is blocked.

## Crash-boundary evidence

The Phase 4 acceptance suite uses real Python subprocesses and `os._exit()` to prove:

- pause, cancellation, workload kill, and global kill survive restart;
- denied execution allocates no worker/effect attempt and makes no external call;
- worker/effect attempt exhaustion blocks before allocation;
- crash after spend reservation/before attempt reuses the same reservation;
- crash after worker attempt allocation leaves reservation outstanding and Phase 2 ambiguity blocks replay;
- crash after remote effect acceptance leaves reservation outstanding and Phase 3 ambiguity blocks replay;
- Phase 3 reconciliation can resolve the remote effect without duplicate execution;
- policy hashes remain attributable across restart.

## Operator evidence

`argus status` and `argus inspect` expose machine-readable mission/step, schedule, worker attempt, effect lineage, guardrail, and budget evidence. Raw consumer payloads, receipt evidence, and full worker output are not emitted by default.

Operator CLI primitives also expose guardrail policy initialization, pause/resume/cancel, kill switches, and budget initialization.

## Next architecture target — Phase 5 remote always-on runtime

The next generic runtime concern is deployment rather than another local control primitive: service/daemon execution, remote CLI transport, secure runtime configuration, health/liveness, and backup/restore while preserving Phase 1–4 semantics.

Before generalizing further, the Digital Assets Lab reference workload should consume the completed Phase 1–4 contracts end to end. Remote deployment must not weaken durability, effect reconciliation, or execution gates.

## Human-over-the-loop

ARGUS is designed so humans define policy and supervise missions rather than approve every routine step. Human intervention is reserved for genuine exceptions such as interactive credentials, unresolved policy ambiguity, exhausted budgets requiring a policy change, or external effects that cannot be reconciled safely.

## Architecture rule for future contributions

Before adding a capability to ARGUS, ask:

> Would this capability still make sense if the first consumer were not Digital Assets Lab?

If the answer is no, it probably belongs in the consumer application.
