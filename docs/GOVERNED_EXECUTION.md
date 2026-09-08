# Governed Execution — Phase 4

`GovernedRuntime` is the Phase 4 execution surface for continuous unattended work.

It composes the already-delivered Phase 2 worker runtime and Phase 3 effect runtime without weakening their recovery contracts.

## Why a separate governed surface exists

The lower-level Phase 2 and Phase 3 runtimes remain useful primitives for tests, migrations, and explicit consumer composition. They predate Phase 4 and therefore do not silently acquire policy requirements.

Once a mission opts into Phase 4 and is intended to run unattended, execution should enter through `GovernedRuntime`.

This keeps the compatibility boundary explicit:

```text
Phase 2/3 primitive
    -> recovery semantics

Phase 4 GovernedRuntime
    -> durable control gate
    -> attempt budget gate
    -> optional spend reservation
    -> Phase 2/3 primitive
```

## Worker gate

Before `AttemptStore.begin_attempt()` can happen, `GovernedRuntime.execute_due_worker()` performs:

1. durable due/ambiguity preflight;
2. mission control check;
3. global/workload kill check;
4. worker-attempt budget check;
5. spend-budget check when configured;
6. durable spend reservation when configured;
7. structured governed-execution audit;
8. only then Phase 2 attempt allocation and worker invocation.

If a configured spend budget exists, the caller must provide an explicit reservation upper bound. A worker-reported trusted `cost_usd` commits actual spend after the invocation. If actual spend is unavailable or execution becomes ambiguous, the reservation remains outstanding and continues to consume budget.

## Effect gate

Before an effect can cross `INTENT_COMMITTED -> OUTCOME_UNKNOWN`, `GovernedRuntime.execute_effect()` performs the same control checks using the separate effect-attempt budget.

A confirmed-not-applied re-attempt must pass the gate again and consumes the next effect-attempt slot.

If an effect is already ambiguous, the governed runtime blocks before creating another reservation or effect attempt. Phase 3 reconciliation remains the only path that can resolve the ambiguity.

## Spend settlement for effects

ARGUS does not infer consumer-specific effect cost from opaque evidence. A consumer may provide a generic cost resolver for a trustworthy terminal `EffectRuntimeResult`. Without trustworthy cost evidence, the reservation remains durable rather than being optimistically released.

## Crash behavior

The ordering is deliberate:

```text
ALLOW controls/budget
    -> reserve spend (when configured)
        -> allocate durable attempt/effect boundary
            -> invoke external work
```

Therefore:

- crash after reservation but before attempt allocation leaves the same deterministic reservation available for idempotent reuse;
- crash after worker attempt allocation leaves a Phase 2 `STARTED` ambiguity and the reservation remains outstanding;
- crash after effect boundary leaves Phase 3 `OUTCOME_UNKNOWN` and the reservation remains outstanding;
- restart does not create a new reservation before those ambiguity checks are resolved.

## Policy requirements

`GovernedRuntime` requires both a durable guardrail policy and a durable budget policy for the mission. A budget policy may leave individual limits unset, but its version/hash still provides explicit policy provenance.

## Audit

Every governed allow/deny decision records:

- execution kind (`worker` or `effect`);
- decision and reason;
- guardrail policy hash;
- budget policy hash;
- spend reservation key when one was allocated;
- timestamp.

Read-only polling outside the governed execution boundary does not allocate attempts or consume spend.

## Scope

This Phase 4 slice is intentionally single-process. It does not claim distributed leases or atomic cross-process reservations. Consumer business policy and domain semantics remain outside ARGUS.
