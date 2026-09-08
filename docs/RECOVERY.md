# ARGUS Restart and Recovery Semantics

ARGUS treats restart behavior as part of correctness. Phase 1 proves durable logical-step recovery, Phase 2 extends that contract across future scheduling and bounded worker attempts, and Phase 3 extends it across ambiguous external side effects.

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

The Phase 3 acceptance suite proves this with a fake remote system persisted in a file separate from ARGUS SQLite and a real `os._exit()` after remote acceptance.

## 11. Consumer-owned reconciliation

For an ambiguous effect, ARGUS invokes a typed consumer reconciler. The consumer inspects its remote system and returns exactly one decision:

- `CONFIRMED_APPLIED`;
- `CONFIRMED_NOT_APPLIED`;
- `STILL_UNKNOWN`.

ARGUS validates correlation against the durable parent effect and current attempt identity. Platform-specific lookup semantics remain entirely consumer-owned.

### Confirmed applied

ARGUS commits an authoritative reconciliation receipt and advances without another external call.

```text
OUTCOME_UNKNOWN
      ↓ reconcile
CONFIRMED_APPLIED
      ↓
replay permanently forbidden
```

### Confirmed not applied

ARGUS commits a not-applied reconciliation receipt. This is the **only** reconciliation outcome that may authorize a new external effect attempt.

A policy-bounded re-attempt receives a new monotonic attempt identity while preserving the stable parent effect correlation.

### Still unknown

```text
STILL_UNKNOWN -> BLOCKED
```

Repeated restarts or reconciliation calls may re-check the remote system, but no new external attempt is allocated while uncertainty remains.

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

The Phase 3 acceptance suite includes this crash boundary.

## 14. What Phase 3 guarantees

In the current local single-process model, ARGUS guarantees:

- durable effect intent precedes any external call;
- parent effect identity and attempt lineage survive restart;
- ambiguity is represented explicitly before the runtime leaves the local transactional boundary;
- ambiguous external outcomes are never blindly replayed;
- confirmed applied outcomes advance without duplicate execution;
- confirmed not-applied outcomes are the only path to a policy-authorized re-attempt;
- still-unknown outcomes remain fail-closed;
- effect replay is independent from Phase 2 worker retry classification;
- duplicate restart/polling does not allocate duplicate active effect attempts;
- safe internal lineage repair never requires an external replay.

ARGUS does **not** claim universal exactly-once external effects. If a remote system cannot expose sufficient evidence to distinguish applied from not applied, the correct state remains blocked/unknown.

## 15. Evidence

`argus status` and `argus inspect` expose machine-readable recovery evidence including:

- mission and logical step states;
- lifecycle journal;
- schedules and worker attempts;
- effect IDs and stable parent correlation keys;
- effect lifecycle state;
- receipt outcome and source;
- effect-attempt number and attempt key;
- effect/reconciliation lineage.

Raw consumer payloads, receipt evidence, and full worker stdout/stderr are not emitted by default.

## 16. Corruption

Unsupported schema versions, malformed persisted timestamps/JSON, inconsistent effect state, invalid correlation relationships, and invalid SQLite state fail explicitly rather than being heuristically repaired.

## Next recovery target — Phase 4

Phase 4 will add durable operator and budget gates around execution. Recovery acceptance must prove that a crash cannot bypass a pause, cancellation, kill switch, or budget reservation/exhaustion boundary.
