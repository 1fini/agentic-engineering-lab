# ARGUS Phase 1 Durable State Contract

This document describes the durable state semantics implemented by Mission #1 workstream #3.

The persistence implementation is local SQLite, as selected by ADR-0001. Consumer payloads are opaque JSON objects from ARGUS's perspective.

## Storage boundary

A mission store contains four logical records:

- `argus_metadata` — storage schema version;
- `missions` — durable mission identity and state;
- `steps` — ordered versioned step envelopes and optional terminal results;
- `journal` — append-only creation/state-transition history.

State transitions and their journal entries are committed in the same SQLite transaction. A failed journal write therefore cannot leave a committed state transition without its audit record.

## Mission states

```text
PENDING -> RUNNING -> COMPLETED
    |         |
    +-------> FAILED
```

`COMPLETED` and `FAILED` are terminal in Phase 1. A mission cannot enter `COMPLETED` while any persisted step is not `SUCCEEDED`.

## Step states

```text
PENDING -> RUNNING -> SUCCEEDED
    |         |
    +-------> FAILED
```

`SUCCEEDED` and `FAILED` are terminal. A succeeded step requires a durable `SUCCESS` result; a failed step requires a durable `FAILURE` result. Non-terminal steps cannot carry a terminal result.

Re-applying the exact same already-committed state/result is an idempotent no-op and does not append another journal entry. Supplying a different result for an already-committed terminal state fails closed.

## Step envelope v1

Each step is defined by a versioned generic envelope containing:

- mission id;
- step id;
- zero-based ordinal;
- operation name;
- opaque JSON payload;
- payload version;
- envelope schema version;
- deterministic idempotency key.

The idempotency key is a SHA-256 digest over canonical JSON of the versioned step definition. JSON object key ordering does not affect the key.

ARGUS does not interpret domain fields inside the payload.

## Result v1

A terminal step result contains:

- result schema version;
- result kind (`success` or `failure`);
- opaque JSON output.

## Corruption and version handling

ARGUS does not guess through malformed durable state.

Opening or reading the store fails explicitly when it encounters, among other cases:

- an unsupported storage/mission/step/result schema version;
- missing required tables or storage version metadata;
- invalid JSON payload/result data;
- unknown persisted enum states;
- inconsistent terminal state/result combinations;
- non-contiguous persisted step ordinals;
- an idempotency key that no longer matches the persisted step definition;
- a file that is not a valid SQLite database.

Schema migration is intentionally out of scope for the first runtime slice. A future version must add an explicit migration contract rather than silently rewriting persisted data.

## What this workstream does not yet do

The durable store does not select or execute steps. It has no scheduler, OpenCode integration, worker retries, recovery policy for `RUNNING` work, or external side-effect semantics. Those behaviors belong to later workstreams/missions and must build on—not bypass—the durable state contract.
