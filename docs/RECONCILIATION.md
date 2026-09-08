# ARGUS Effect Execution and Reconciliation Boundary

Mission #21 workstream #23 composes the durable effect state from `docs/EFFECTS.md` with consumer-owned execution and reconciliation adapters.

## Separation of responsibilities

ARGUS owns:

- when an effect is allowed to cross the durable ambiguity boundary;
- durable effect identity/correlation;
- typed execution/reconciliation outcomes;
- correlation validation;
- fail-closed replay behavior;
- receipt persistence.

Consumers own:

- how the external operation is performed;
- remote API credentials and semantics;
- what evidence proves that an effect was applied or not applied;
- how a remote system is queried during reconciliation.

ARGUS does not infer external outcomes from logs, prose, model guesses, or domain heuristics.

## Execution contract

`EffectRuntime.execute_effect()` requires a durable `INTENT_COMMITTED` effect.

Before consumer code is invoked, ARGUS transactionally crosses:

```text
INTENT_COMMITTED -> OUTCOME_UNKNOWN
```

Only then does ARGUS call the consumer-provided `EffectExecutor`.

The executor returns a typed `EffectExecutionResult` correlated to the exact durable effect identity:

- `APPLIED` -> commit an execution receipt and `CONFIRMED_APPLIED`;
- `NOT_APPLIED` -> commit an execution receipt and `CONFIRMED_NOT_APPLIED`;
- `UNKNOWN` -> leave `OUTCOME_UNKNOWN` and block effect replay.

If the executor raises, the process dies, returns an invalid type, or returns a mismatched correlation key, the durable effect remains `OUTCOME_UNKNOWN`. A later `execute_effect()` call is refused until reconciliation resolves the ambiguity.

## Reconciliation contract

`EffectRuntime.reconcile_effect()` is legal only for an `OUTCOME_UNKNOWN` effect.

The consumer-provided `EffectReconciler` receives the durable effect record and returns a typed, correlated decision:

- `CONFIRMED_APPLIED` -> persist a reconciliation receipt and advance without executing the effect again;
- `CONFIRMED_NOT_APPLIED` -> persist a reconciliation receipt; later workstream #24 may decide whether a new effect attempt is allowed;
- `STILL_UNKNOWN` -> leave the effect blocked/fail-closed.

Repeated reconciliation while still unknown is allowed. Repeated external execution while unknown is not.

## Important distinction: computational retry vs effect replay

Phase 2 worker retries answer:

> Is it safe to run this bounded computation again?

Phase 3 effect reconciliation answers:

> Did this non-idempotent external operation already happen?

They are deliberately different contracts. A Phase 2 timeout/retryable failure never authorizes replay of a Phase 3 external effect.

## Correlation

Execution and reconciliation responses must carry the exact durable effect correlation key. Mismatched identity is a contract error and leaves the effect in its safe current state.

## Scope boundary

This workstream does not yet implement:

- multiple effect attempts after a confirmed non-application;
- duplicate-prevention/lineage across those attempts;
- real subprocess crash-matrix acceptance;
- consumer-specific remote reconciliation;
- budgets, pause/resume, cancellation, or kill switch.

Those are #24, #25, consumer responsibilities, or later ARGUS phases.
