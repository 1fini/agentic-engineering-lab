# ARGUS Restart and Recovery Semantics

ARGUS treats restart behavior as part of correctness. Phase 1 proves durable logical-step recovery; Phase 2 extends that contract across future scheduling and external worker attempts.

## 1. Committed logical step

If a process dies **after** a step's terminal result and journal transitions are committed, that durable state is authoritative.

On restart ARGUS validates the same SQLite state, observes the terminal step, skips it, and continues only with later eligible work.

A durably completed step is not re-executed merely because the orchestrator process died.

## 2. Phase 1 `RUNNING` step after process death

A Phase 1 callable worker may die after ARGUS commits `PENDING -> RUNNING` but before a durable result exists.

That boundary is ambiguous:

```text
RUNNING after restart -> BLOCKED
```

ARGUS does not automatically reset the step and does not blindly invoke the worker again.

## 3. Future `due_at` survives restart

Phase 2 stores future eligibility in the same durable SQLite state file.

A process can exit while a step is waiting. A later process reopening the state sees:

- no eligibility before `due_at`;
- eligibility at/after `due_at` if the step is still `PENDING` and its ordered dependencies are satisfied.

The acceptance suite probes this with separate Python interpreter processes before and at the due boundary.

Repeated due scans are read-only and create no attempt or worker invocation by themselves.

## 4. Durable attempt boundary

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

If restart sees a `STARTED` attempt with no durable outcome, ARGUS cannot prove whether the external computation or effect happened. It therefore blocks automatic replay.

The Phase 2 acceptance suite creates the attempt in a child process and terminates it with `os._exit()`, then proves a restarted runtime refuses to invoke the worker again.

## 5. Proven timeout vs termination uncertainty

A bounded worker timeout has two materially different outcomes.

### Process tree termination established

On POSIX, ARGUS uses a dedicated process group and attempts TERM followed by KILL within bounded grace periods.

If termination is established, the invocation is durably classified `timeout`. Retry policy may explicitly permit a later attempt.

### Termination cannot be established

If ARGUS cannot prove the launched process tree is gone, the outcome is `termination_uncertain`.

```text
termination_uncertain -> BLOCKED
```

ARGUS does not treat uncertainty as a normal retryable timeout.

## 6. Retryable completed attempt

A validated worker may declare `retryable_failure`, or deterministic policy may classify a proven timeout/process error as retryable.

If the attempt limit is not exhausted:

1. the current attempt is durably `COMPLETED` with its classification;
2. the logical step remains `PENDING`;
3. a new future `due_at` is persisted;
4. no retry is allowed before that boundary;
5. the next invocation receives a new monotonic attempt number/request ID.

When an explicit logical clock is injected for deterministic execution/tests, Phase 2 uses that same boundary to compute retry timing.

## 7. Attempt exhaustion or permanent failure

Permanent failure, malformed output, non-retryable transport failure, or retry exhaustion terminalizes the logical step as `FAILED` and the mission as `FAILED`.

The active schedule entry is cleared while audit history remains inspectable.

## 8. Final step committed, mission completion not yet committed

There is a narrow **safe** crash boundary after the final logical step is durably `SUCCEEDED` but before mission state reaches `COMPLETED`.

Unlike an unknown worker effect, this ambiguity is entirely internal and can be reconciled from durable state:

```text
all logical steps SUCCEEDED + mission RUNNING
        -> mission COMPLETED
```

`Phase2Runtime.reconcile_mission()` performs that repair without invoking any worker.

## Evidence

`argus status` and `argus inspect` expose machine-readable recovery evidence including:

- mission and logical step states;
- deterministic step idempotency keys;
- lifecycle journal;
- active `due_at` values and schedule history;
- attempt number and request ID;
- attempt state/outcome;
- start/completion timestamps;
- duration and exit code;
- optional token/cost usage;
- retry due time.

Raw consumer payloads and full worker stdout/stderr are not emitted by default.

## Corruption

Unsupported schema versions, malformed persisted timestamps/JSON, inconsistent states, invalid result relationships, and invalid SQLite state fail explicitly rather than being repaired heuristically.

## Guarantees through Phase 2

ARGUS guarantees in the current local single-process model:

- durably completed logical steps are not replayed after restart;
- future scheduling survives restart;
- a durable ambiguous attempt blocks blind replay;
- duplicate due scans do not themselves duplicate execution;
- deterministic retry policy is preserved in durable attempt/schedule evidence;
- safe internal mission-completion ambiguity can be reconciled without worker replay.

ARGUS does **not** yet guarantee exactly-once external side effects.

That requires Phase 3 durable effect intent/receipt/reconciliation semantics. Until those exist, ambiguity around an external effect remains a hard block rather than a reason to retry optimistically.
