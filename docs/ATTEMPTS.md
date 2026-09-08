# ARGUS durable attempts and retry semantics

Mission #10 workstream #13 adds a durable attempt layer between future eligibility and external worker invocation.

## Why attempts are separate from core step state

A Phase 1 step has a deliberately small state machine:

```text
PENDING -> RUNNING -> SUCCEEDED | FAILED
```

A Phase 2 external worker may fail transiently several times before the logical step reaches a terminal result. Reusing core `RUNNING` for every external invocation would require an unsafe `RUNNING -> PENDING` reset after retryable failures and would blur the distinction between active/ambiguous work and logical step completion.

ARGUS therefore keeps the logical core step `PENDING` while individual external invocations are represented as durable attempts.

## Attempt lifecycle

```text
scheduled step becomes due
        |
        v
attempt STARTED committed
        |
        +--> process/runtime dies -> replay BLOCKED
        |
        v
bounded worker invocation
        |
        v
attempt COMPLETED + classified outcome committed
        |
        +--> success -> logical step terminal SUCCEEDED
        +--> retryable -> step stays PENDING, due_at is rescheduled
        +--> permanent/exhausted -> logical step terminal FAILED
        +--> termination_uncertain -> replay BLOCKED
```

Every worker invocation must have a committed `STARTED` attempt before the process is launched. If ARGUS dies after that commit and before it can persist a validated outcome, the attempt remains `STARTED`. A later execution refuses to replay it automatically because the external computation may have run.

This is intentionally conservative. Phase 3 will add explicit side-effect reconciliation for workloads that can safely resolve ambiguous effects.

## Durable attempt metadata

An attempt records:

- mission and step identity;
- monotonically increasing attempt number;
- deterministic request id (`mission/step/attempt-N`);
- `STARTED` / `COMPLETED` state;
- start/completion timestamps;
- classified invocation outcome;
- duration;
- exit code when available;
- optional input/output token counts and cost reported by the validated worker response;
- retry due time when a retry is scheduled;
- a bounded generic diagnostic detail.

Raw worker requests, raw stdout, raw stderr, and worker response output are not stored in the attempt table. Validated successful output becomes the normal durable logical `StepResult`, where the consumer contract expects it.

## Retry policy

`AttemptPolicy` is deterministic code. It currently controls:

- `max_attempts`;
- default retry delay;
- whether process-start/non-zero-exit errors are retryable;
- whether proven process-tree timeouts are retryable.

Worker-declared `retryable_failure` is retryable. A validated `retry_after_seconds` may replace the default delay for that attempt.

The following outcomes are not retried automatically:

- worker-declared `permanent_failure`;
- malformed worker output;
- output-limit violation;
- process-tree termination uncertainty.

When a retryable outcome reaches `max_attempts`, the logical step fails with an explicit `attempt_limit_exhausted:<outcome>` result.

## Atomic terminalization

While an external attempt runs, the core logical step stays `PENDING`. After a final success/failure is known, ARGUS preserves the Phase 1 transition history by recording:

```text
PENDING -> RUNNING -> SUCCEEDED | FAILED
```

Both logical transitions and the final result are applied within the same SQLite transaction that completes the attempt. The intermediate `RUNNING` state is therefore a logical journal transition, not a separately observable durable state.

A retryable attempt does not touch core step state; it atomically completes the attempt and reschedules the existing `due_at`.

## Scheduler interaction

- execution is allowed only when `DurableScheduler.due()` says the step is eligible;
- retry decisions persist a new future `due_at`;
- final step decisions clear the active schedule while retaining schedule history;
- duplicate scans do not create attempts by themselves;
- an open/ambiguous attempt prevents the executor from creating another attempt.

## Inspectability

`argus status` reports attempt count plus the latest attempt state/outcome per step.

`argus inspect` reports durable attempt metadata including duration, exit code, token/cost usage, and retry due time. It intentionally omits worker request payloads and response outputs.

## Scope boundary

This layer does not yet provide:

- automatic reconciliation of an ambiguous started attempt;
- exactly-once external side effects;
- spend budgets or reservations;
- pause/cancel/kill switch;
- distributed attempt leasing;
- consumer business semantics.

Those are later ARGUS phases or consumer responsibilities.
