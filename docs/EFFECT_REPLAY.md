# ARGUS Safe Effect Re-attempt and Lineage

Mission #21 workstream #24 adds a separate durable attempt model for external effects.

## Why effect attempts are not worker attempts

Phase 2 worker attempts track bounded computation. Phase 3 effect attempts track calls that may change an external system.

Those histories must never be conflated. A retryable worker error does not authorize replay of a non-idempotent external effect.

## Parent effect identity and attempt identity

Every effect keeps one stable parent correlation key derived from its durable intent.

Each external effect call gets a monotonic attempt identity:

```text
<effect-correlation>:attempt:1
<effect-correlation>:attempt:2
...
```

The consumer receives both identities in `EffectExecutionContext`. If a remote system supports an idempotency/correlation token, the consumer may map these generic identities onto the platform mechanism it owns.

## First attempt

The first effect attempt is reconstructed from the durable `execution_boundary_entered` event introduced in WS1. This allows stores created before the dedicated attempt table existed to be extended without discarding the already-durable ambiguity evidence.

The attempt is synchronized from the parent effect receipt after a trusted execution or reconciliation result is committed.

## Safe re-attempt rule

A second external call is legal only after the previous effect attempt is durably `CONFIRMED_NOT_APPLIED`.

```text
CONFIRMED_NOT_APPLIED
        |
        | explicit reattempt_effect + policy
        v
new effect attempt N+1
parent effect -> OUTCOME_UNKNOWN
        |
        v
consumer executor
```

`EffectReplayPolicy.max_effect_attempts` provides a deterministic cap.

The following states can never authorize a new external call:

- `CONFIRMED_APPLIED`;
- `OUTCOME_UNKNOWN`;
- `INTENT_COMMITTED` through the re-attempt API.

## Duplicate prevention

Authorizing a re-attempt and moving the parent effect back to `OUTCOME_UNKNOWN` occur in the same SQLite transaction that creates the new effect-attempt record.

Therefore a second poll/restart after that commit sees ambiguity and cannot allocate another attempt.

## Repairable internal boundary

A process may die after a parent effect receipt is committed but before the dedicated effect-attempt row is synchronized. This is an internal, inspectable boundary: reopening `EffectAttemptStore` repairs an open attempt from the already-authoritative parent receipt without making another external call.

This repair does not infer remote state. It only copies already-durable local receipt evidence into the lineage table.

## Inspectability

`argus status` exposes effect-attempt counts per step.

`argus inspect` exposes:

- effect id;
- attempt number;
- attempt key;
- attempt state;
- start/resolution timestamps;
- receipt outcome/source.

Raw effect payloads and receipt evidence remain omitted.

## Scope boundary

This workstream does not yet provide the final subprocess crash matrix or a real consumer/platform reconciliation adapter. Mission #21 workstream #25 owns that acceptance proof.
