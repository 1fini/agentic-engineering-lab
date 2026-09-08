# ARGUS Budgets and Spend Reservations

Phase 4 budgets are deterministic runtime constraints. They are not prompt guidance and they are not delegated to a model.

## Scope of WS2

This workstream provides the durable budget/accounting primitives only. Wiring them into Phase 2 worker and Phase 3 effect execution is owned by the next workstream under Mission #30.

## Versioned budget policy

A mission may attach a generic `BudgetPolicy` with:

- monotonic policy version;
- optional maximum number of worker attempts;
- optional maximum number of external-effect attempts;
- optional spend limit in USD.

The policy has a deterministic `argus:budget:v1:*` hash. Policy updates require a strictly increasing version.

Attempt and spend limits are independent. A missing limit means that dimension is not bounded by this policy.

## Attempt budgets

ARGUS does not maintain duplicate counters for attempts. Consumption is derived from the authoritative durable evidence already owned by prior phases:

- worker attempts come from the Phase 2 `attempts` table;
- effect attempts come from the Phase 3 `effect_attempts` table.

An allocated attempt therefore counts even if its eventual outcome is ambiguous. This is intentional: a crash cannot make a consumed attempt disappear from the budget.

Budget checks are read-only and do not allocate attempts or mutate the budget ledger.

## Spend budget

Spend accounting uses decimal values and a durable reservation protocol:

```text
available capacity
       |
       v
RESERVED before cost-bearing call
       |
       +--> trustworthy actual cost -> COMMITTED
       |
       +--> trustworthy no-spend path -> RELEASED
       |
       +--> process death / ambiguity -> remains RESERVED
```

### Reserve before invocation

A cost-bearing operation must reserve capacity before the call begins. Outstanding reservations count against available spend immediately.

The reservation has a stable caller-supplied key. Repeating an identical reserve operation is idempotent; reusing the key with different semantics is rejected.

### Commit trustworthy cost

A committed actual cost may be lower than the reservation but cannot exceed it. Once committed, only the actual amount counts as spent; unused reserved capacity becomes available again.

An identical repeated commit is idempotent. A committed reservation cannot later change its actual amount or be released.

### Release only from trustworthy evidence

A reservation can be released only while still `RESERVED`. Releasing it twice is idempotent. A released reservation can never later be committed.

Phase 4 execution integration is responsible for deciding when a trustworthy outcome justifies commit or release.

### Crash safety

ARGUS never automatically releases a reservation merely because the process restarted. An outstanding reservation remains budget-consuming until deterministic evidence authorizes commit or release.

This is conservative by design: uncertainty may temporarily reduce available capacity, but cannot silently produce overspend.

## Available spend

```text
available = limit - committed - outstanding reservations
```

Duplicate checks do not consume budget. The reservation write re-checks available capacity inside the SQLite transaction before inserting the reservation.

## Evidence

`status` exposes the current budget snapshot when a policy exists:

- policy version/hash;
- worker-attempt limit and used count;
- effect-attempt limit and used count;
- spend limit;
- committed spend;
- outstanding reserved spend;
- available spend.

`inspect` additionally exposes reservation lifecycle and append-only budget events. Consumer step payloads are not included.

## CLI

```bash
argus budget-init --store .argus/state.db \
  --worker-attempt-limit 20 \
  --effect-attempt-limit 5 \
  --spend-limit-usd 10.00 \
  <mission-id>
```

The direct Python store API supplies reserve/commit/release operations. A separate CLI for manual reservation mutation is intentionally not added in WS2; execution-path integration should own those lifecycle transitions.

## What WS2 does not yet guarantee

Budget checks do not yet sit immediately before worker/effect attempt allocation. That composition is required before Phase 4 can claim that an exhausted budget prevents execution.

Likewise, a spend reservation does not itself invoke or account for a worker/effect. The next workstream composes guardrail controls, attempt limits, reservations, and the existing Phase 2/3 execution boundaries.
