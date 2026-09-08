# ARGUS Durable External-Effect State

Mission #21 workstream #22 introduces the Phase 3 durable state boundary for external effects.

## Purpose

Phase 2 can safely retry bounded computation, but a non-idempotent external operation is different. If ARGUS crashes after a remote system accepts an operation but before ARGUS records the result, replay may duplicate the effect.

Phase 3 therefore starts with a stricter invariant:

> Persist intent before effect, and treat the interval between the execution boundary and a trustworthy receipt as ambiguous.

## Effect identity

An `EffectIntent` is generic and versioned. It contains:

- mission id;
- step id;
- effect id;
- generic operation name;
- opaque JSON payload;
- payload version;
- deterministic correlation key.

The correlation key is derived from canonical JSON of the durable intent and survives process restart. It is an ARGUS identity/correlation primitive, not a claim that an external system supports idempotency.

## Phase 3 state model introduced by WS1

```text
INTENT_COMMITTED
      |
      | begin_execution() -- committed immediately before external call
      v
OUTCOME_UNKNOWN
      |
      +--> trustworthy APPLIED receipt     -> CONFIRMED_APPLIED
      |
      +--> trustworthy NOT_APPLIED receipt -> CONFIRMED_NOT_APPLIED
```

`OUTCOME_UNKNOWN` is deliberately conservative. Once ARGUS crosses that durable boundary, a restart must assume the external call may have happened until a consumer-owned reconciliation path proves otherwise.

Calling `begin_execution()` again while an effect is `OUTCOME_UNKNOWN` raises an ambiguity error rather than permitting blind replay.

## Durable receipts

A receipt is versioned and correlated to the exact durable effect identity. It contains:

- correlation key;
- `APPLIED` or `NOT_APPLIED` outcome;
- source (`EXECUTION` or future `RECONCILIATION`);
- opaque evidence object.

Receipt evidence is persisted because later reconciliation/recovery needs durable evidence, but the default CLI does not print the raw evidence or effect payload.

A receipt commit and its effect-history transition occur in one SQLite transaction. Repeating the exact same terminal receipt is idempotent; attempting to replace a terminal receipt with a different outcome/evidence fails closed.

## Why `OUTCOME_UNKNOWN` is entered before the call

There is no atomic transaction spanning SQLite and an arbitrary external system. Therefore ARGUS cannot safely distinguish these two events after process death:

```text
local state says execution may start
    -> process dies before remote call
```

and:

```text
local state says execution may start
    -> remote accepts effect
    -> process dies before local receipt
```

The safe choice is to commit `OUTCOME_UNKNOWN` immediately before leaving the local transactional boundary. A crash after intent but before this boundary is safe to resume. A crash after the boundary requires reconciliation.

## Persistence

The Phase 3 extension uses the same SQLite state file and adds versioned tables for:

- effect records;
- receipt fields;
- append-only effect events;
- effect-extension schema metadata.

The Phase 1 mission/step schema is not rewritten. Existing Phase 1/2 stores are extended lazily and remain readable.

## Inspectability

`argus status` adds per-step effect count/state summaries.

`argus inspect` exposes:

- effect id;
- operation;
- correlation key;
- durable state;
- receipt outcome/source when present;
- timestamps;
- append-only effect history.

It intentionally omits raw effect payloads and receipt evidence.

## Scope boundary

WS1 does **not** yet implement:

- an external-effect executor adapter;
- automatic reconciliation;
- safe re-attempt after `CONFIRMED_NOT_APPLIED`;
- duplicate-prevention across multiple effect attempts;
- exactly-once external effects;
- budgets, pause/resume, cancellation, or kill switch;
- consumer/platform semantics.

Those are owned by later workstreams under Mission #21 or later ARGUS phases.
