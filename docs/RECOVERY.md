# ARGUS Restart and Recovery Semantics

ARGUS treats restart behavior as part of correctness. Phase 1 proves durable logical-step recovery, Phase 2 extends that contract across future scheduling and bounded worker attempts, Phase 3 extends it across ambiguous external side effects, and Phase 4 extends it across operator controls and budget boundaries.

## 1. Committed logical step

If a process dies after a step's terminal result and journal transitions are committed, that durable state is authoritative. On restart ARGUS skips the completed step and continues only with later eligible work.

## 2. Phase 1 `RUNNING` step after process death

A Phase 1 callable worker may die after ARGUS commits `PENDING -> RUNNING` but before a durable result exists.

```text
RUNNING after restart -> BLOCKED
```

ARGUS does not automatically reset or replay it.

## 3. Future `due_at` survives restart

Phase 2 stores future eligibility durably. A restarted process sees no eligibility before `due_at` and eligibility at/after the boundary if the step is still pending and ordered dependencies are satisfied. Repeated due scans are read-only.

## 4. Durable computational attempt boundary

Before invoking a Phase 2 external worker, ARGUS commits an attempt in `STARTED` state.

```text
attempt STARTED committed
        |
        +--> process dies / result unknown
        |       -> replay BLOCKED
        |
        v
bounded worker returns classified outcome
        |
        v
attempt COMPLETED committed
```

A durable `STARTED` attempt without a trustworthy outcome blocks replay.

## 5. Proven timeout vs termination uncertainty

If process-tree termination is established, timeout may be retryable under explicit deterministic policy. If termination cannot be established, the outcome is `termination_uncertain` and automatic replay is blocked.

## 6. Retryable completed worker attempt

A completed retryable worker attempt may schedule a future `due_at` until its deterministic attempt limit is reached. Worker retries are computational retries only; they never authorize replay of an external side effect.

## 7. Final logical step committed, mission completion not yet committed

If all logical steps are durably succeeded but mission state is still running, ARGUS can safely reconcile the mission to `COMPLETED` without invoking any worker. This is internal-state repair, not external-effect reconciliation.

## 8. Phase 3: durable external-effect intent

Before any effectful consumer call, ARGUS persists a versioned `EffectIntent` with a stable parent correlation identity.

```text
EffectIntent durable
      ↓
INTENT_COMMITTED
```

A crash after intent commit but before entering the external-call boundary is safe: no effect attempt has started yet, so restart may continue from the durable intent.

## 9. Phase 3: ambiguity is durable before the remote call

Before consumer executor code is invoked, ARGUS allocates the effect attempt and durably transitions the parent effect to `OUTCOME_UNKNOWN`.

```text
INTENT_COMMITTED
      ↓
allocate effect attempt N
      ↓
OUTCOME_UNKNOWN committed
      ↓
consumer external call
```

Any process death, adapter exception, invalid response, or remote uncertainty after this point leaves the effect ambiguous. Restart cannot treat it as an ordinary retryable failure.

## 10. Crash after remote acceptance, before local receipt

This is the critical distributed boundary:

```text
ARGUS: OUTCOME_UNKNOWN
remote: effect may already be applied
local receipt: missing
```

On restart:

```text
blind replay -> FORBIDDEN
reconciliation -> REQUIRED
```

The Phase 3 acceptance suite proves this with a fake remote system persisted separately from ARGUS SQLite and a real `os._exit()` after remote acceptance.

## 11. Consumer-owned reconciliation

For an ambiguous effect, ARGUS invokes a typed consumer reconciler. The consumer inspects its remote system and returns exactly one decision:

- `CONFIRMED_APPLIED`;
- `CONFIRMED_NOT_APPLIED`;
- `STILL_UNKNOWN`.

ARGUS validates correlation against the durable parent effect and current attempt identity. Platform-specific lookup semantics remain entirely consumer-owned.

Confirmed applied advances without another external call. Confirmed not applied is the only state that may authorize a bounded re-attempt. Still unknown remains blocked.

## 12. Effect-attempt lineage

External-effect attempts are intentionally separate from Phase 2 worker attempts.

```text
parent effect correlation
    ├── effect attempt 1
    │      └── execution/reconciliation outcome
    └── effect attempt 2   # only after confirmed not applied
           └── execution/reconciliation outcome
```

A confirmed applied effect permanently forbids another attempt. Duplicate polling/restart cannot allocate two active effect attempts in the current local single-process model.

## 13. Crash after receipt commit

If the authoritative parent receipt is committed but an auxiliary lineage synchronization step is interrupted, restart repairs the lineage from already-committed local evidence. It does **not** execute the external effect again.

## 14. Phase 4: durable operator control survives restart

Guardrail state is separate from logical mission state and is durable:

```text
ACTIVE <-> PAUSED -> CANCELLED
```

A paused mission remains paused after process restart. New governed execution is denied without allocating a worker/effect attempt. Resume returns the mission to active eligibility without replaying previously completed work.

Cancellation is terminal. Restart cannot implicitly reactivate it.

Workload and global kill switches are also durable. A later interpreter observes the same kill state before any governed attempt allocation.

## 15. Phase 4: denied gates do not mutate execution state

The governed control precedence is deterministic:

```text
cancelled
  -> global kill
  -> workload kill
  -> paused
  -> attempt budget
  -> spend reservation
  -> execution
```

If a control or attempt-budget gate denies execution, no new worker attempt, effect attempt, or external call is created.

Read-only polling/checking does not consume attempts or spend.

## 16. Phase 4: attempt budgets survive restart by using authoritative evidence

Worker-attempt consumption is derived from durable Phase 2 `attempts`; effect-attempt consumption is derived from Phase 3 `effect_attempts`.

This means an allocated attempt still counts after a crash, including an ambiguous one. ARGUS does not depend on a separate mutable counter that could drift from the execution journal.

Exhaustion is checked before the next attempt is allocated.

## 17. Phase 4: spend reservation precedes cost-bearing execution

When a spend limit is configured, governed execution requires an explicit reservation upper bound.

```text
budget capacity available
      ↓
RESERVED committed
      ↓
worker/effect attempt boundary
      ↓
external work
      ↓
trusted cost known? ---- yes ---> COMMITTED(actual)
      |
      no / ambiguous
      ↓
remain RESERVED
```

Outstanding reservations count against available budget across restart. They are never automatically released merely because the ARGUS process died.

## 18. Crash after reservation, before attempt allocation

This is a Phase 4-specific recovery boundary:

```text
spend RESERVED
      ↓
process dies
      ↓
no worker/effect attempt exists yet
```

The next attempt identity is derived from authoritative attempt evidence. Therefore restart derives the same deterministic reservation key and **reuses the existing reservation idempotently**.

The final Phase 4 acceptance initially exposed a bug here: restart checked fresh remaining spend capacity first, saw its own existing reservation, and denied itself as `spend_exhausted`. The delivered implementation fixes the ordering by asking `reserve_spend()` to resolve the same durable reservation key before any fresh-capacity decision.

The acceptance then proves:

```text
existing reservation
      ↓ restart
same reservation key
      ↓
reuse, no second reservation
      ↓
allocate attempt once
      ↓
execute
```

## 19. Crash after spend reservation and worker `STARTED`

If ARGUS dies after the reservation and Phase 2 `STARTED` attempt are durable but before a trustworthy worker outcome:

- the reservation remains outstanding;
- the `STARTED` attempt remains ambiguous;
- restart checks Phase 2 ambiguity before creating another reservation;
- worker replay remains blocked;
- no double reservation occurs.

The Phase 4 acceptance uses a real `os._exit()` inside the worker boundary to prove this behavior.

## 20. Crash after spend reservation and effect remote acceptance

For an effectful operation:

```text
spend RESERVED
      ↓
effect OUTCOME_UNKNOWN
      ↓
remote accepts effect
      ↓
process dies before local receipt
```

Restart observes Phase 3 ambiguity **before** it can allocate another reservation or effect attempt. The existing reservation remains outstanding and governed replay is forbidden.

The consumer reconciler may later confirm the effect applied without replay. Spend settlement remains explicit and must be based on trustworthy evidence; ambiguity does not silently free capacity.

## 21. What Phase 4 guarantees

In the current local single-process model, ARGUS guarantees:

- pause/resume/cancel state survives restart;
- cancellation is terminal;
- workload/global kill switches survive restart;
- denied control or attempt-budget gates allocate no new attempt and make no external call;
- worker/effect attempt budgets are based on authoritative durable attempt evidence;
- spend is reserved before configured cost-bearing governed execution;
- duplicate/restarted reservation for the same next attempt reuses the same durable identity;
- an unresolved reservation continues to consume capacity;
- crash after worker attempt allocation leaves reservation outstanding and Phase 2 ambiguity blocks replay;
- crash after effect attempt/remote acceptance leaves reservation outstanding and Phase 3 ambiguity blocks replay;
- policy hashes remain attributable in governed execution evidence;
- Phase 4 does not weaken any Phase 1–3 recovery guarantee.

ARGUS still does not claim distributed lease safety or universal remote billing reconciliation. Those are outside the current single-process contract.

## 22. Evidence

`argus status` and `argus inspect` expose machine-readable recovery evidence including:

- mission and logical step states;
- lifecycle journal;
- schedules and worker attempts;
- effect IDs, lifecycle, receipts, and effect-attempt lineage;
- mission guardrail policy/control state;
- workload/global kill state;
- budget policy and attempt consumption;
- spend reservations and their lifecycle;
- governed allow/deny decisions with policy provenance.

Raw consumer payloads, receipt evidence, secrets, and full worker stdout/stderr are not emitted by default.

## 23. Corruption

Unsupported schema versions, malformed persisted timestamps/JSON, inconsistent effect state, invalid correlation relationships, invalid guardrail/budget policy hashes, invalid reservation state, and invalid SQLite state fail explicitly rather than being heuristically repaired.

## Next recovery target — Phase 5

Phase 5 will move the runtime into an always-on service environment. Its recovery contract must preserve Phase 1–4 behavior across service/process restarts and deployment operations, including safe runtime configuration, health/liveness, and backup/restore of durable state.

The Digital Assets Lab reference workload should first consume Phase 1–4 end to end so remote deployment is driven by a real continuous mission rather than speculative infrastructure.
