# ARGUS Architecture

## Purpose

ARGUS is the runtime and orchestration layer of Agentic Engineering Lab.

Its job is to make long-running agent missions durable, inspectable, recoverable, policy-bound, and safe to resume. It is not a domain application and it must not absorb business logic from its consumers.

## Architectural boundary

ARGUS owns generic execution concerns:

- mission and step lifecycle;
- durable state;
- scheduling and wakeups;
- bounded worker execution;
- structured input/output validation;
- retry, timeout, and error classification;
- idempotency and recovery coordination;
- correlated logs, events, artifacts, and evidence;
- budgets and policy checks;
- pause, resume, cancellation, and kill switch.

Consumer applications own domain concerns:

- business rules;
- domain-specific state transitions;
- domain APIs and semantics;
- interpretation of domain metrics;
- generation rules;
- publication or transaction rules;
- domain-specific quality and policy checks.

A concrete example:

```text
Digital Assets Lab                     ARGUS
------------------                     -----
YouTube metrics                        durable step execution
Short performance analysis             scheduler / wakeups
editorial hypothesis                   worker invocation
video generation semantics             retries / timeouts
editorial QA                            state persistence
publication policy                     budgets / kill switch
YouTube upload reconciliation          generic side-effect protocol support
```

ARGUS must never contain concepts such as `Short`, `YouTube`, `retention curve`, `editorial hook`, or `video publication policy`.

## Control plane vs cognitive workers

The core design rule is simple:

> The orchestrator is deterministic code. LLMs are bounded workers.

ARGUS owns control flow and state transitions. Workers are invoked for tasks that require reasoning, interpretation, generation, critique, or synthesis.

A worker invocation must be treated like an external computation:

1. inputs are versioned and hashed;
2. execution is bounded by time and budget;
3. output must satisfy a declared schema;
4. malformed output is a typed failure;
5. accepted output is persisted before the mission advances.

## Mission model

A mission is a durable execution contract.

A minimal mission record should eventually contain:

- `mission_id`;
- mission type / version;
- policy version;
- creation time;
- current status;
- current or runnable steps;
- budget envelope;
- correlation identifiers;
- evidence / artifact references;
- pause or terminal reason.

Likely mission statuses:

```text
PENDING
RUNNING
WAITING
PAUSED
BLOCKED
FAILED
COMPLETED
CANCELLED
```

The exact schema remains implementation-defined until the first vertical slice fixes the requirements.

## Step model

A step is the smallest durable unit of work.

The intended execution envelope is conceptually:

```yaml
mission_id: ...
cycle_id: ...
step_id: ...
step_type: ...
contract_version: ...
input_digest: ...
policy_version: ...
attempt: 1
deadline: ...
budget_reservation: ...
output_schema: ...
```

A step result should be typed rather than represented by arbitrary prose.

Candidate result classes:

```text
SUCCESS
WAIT
RETRYABLE_FAILURE
PERMANENT_FAILURE
UNKNOWN_EFFECT
PAUSED_BY_POLICY
```

`UNKNOWN_EFFECT` is especially important for operations that may have succeeded remotely even if the local process did not receive or persist confirmation.

## Side-effect safety

ARGUS must not blindly retry non-idempotent external operations.

For a side-effecting step, the preferred pattern is:

```text
persist intent
    -> execute external call
        -> persist outcome
            -> verify / reconcile if necessary
```

If the process crashes after the external system may have accepted the operation but before a durable local receipt exists, the mission enters an ambiguous state. The next execution must reconcile that state before issuing another side effect.

This is a runtime responsibility at the protocol level. The consumer still owns the domain-specific reconciliation mechanism.

## Scheduling

Long-running missions frequently need to wait for external data or a future condition.

ARGUS therefore needs durable `due_at` scheduling rather than an in-memory sleep loop.

A waiting step should be able to persist:

```text
status = WAITING
next_due_at = <timestamp>
reason = <typed reason>
```

After a process or machine restart, the scheduler must rediscover due work from persistent state.

## Worker execution

The first worker backend is expected to integrate with OpenCode and cloud-hosted models.

The runtime boundary should remain backend-agnostic. A worker adapter should support at least:

- invocation with explicit agent / model configuration;
- structured input;
- process or request timeout;
- output capture;
- structured-output validation;
- cost / token / duration accounting when available;
- process-tree termination on timeout;
- typed provider or execution failure.

The initial implementation may be local-first. A later remote worker model should not require changing mission semantics.

## Persistence

The first implementation should prefer the simplest durable storage that satisfies recovery requirements.

A local embedded database is acceptable for the initial runtime if it can provide:

- transactional writes;
- durable mission and step records;
- uniqueness constraints for idempotency;
- queryable due work;
- migration support;
- crash-safe state transitions.

A distributed database is explicitly not required for the first milestone.

## Observability

Every state transition should be explainable after the fact.

At minimum, ARGUS should eventually record:

- mission ID;
- step ID;
- attempt;
- timestamps;
- input/output digests;
- worker identity / model;
- result type;
- error classification;
- duration;
- cost when available;
- artifact references;
- policy decision;
- correlation IDs.

Logs are useful, but structured persisted events are the more important contract.

## Budgets and operational controls

Autonomy must be bounded.

ARGUS should support mission-level or policy-level limits such as:

- maximum wall-clock duration;
- maximum step attempts;
- maximum worker cost;
- maximum model/token consumption when measurable;
- maximum concurrent work;
- explicit pause state;
- global or workload-specific kill switch.

A budget exhaustion is not an invitation for the model to negotiate with itself. It is a deterministic stop or pause condition.

## Human-over-the-loop

ARGUS is designed for systems where humans set the policy and observe the mission rather than approve routine steps.

A human intervention should be required only when a mission reaches a state that cannot be safely resolved under current policy, for example:

- credentials require interactive consent;
- a policy boundary is ambiguous;
- a non-reconcilable external side effect is uncertain;
- a configured budget has been exhausted;
- the consumer explicitly requests escalation.

Routine completion, retries, scheduled wakeups, and validated worker outputs should not require human approval.

## Deployment direction

Initial target:

```text
local machine
  -> ARGUS runtime
  -> local durable state
  -> OpenCode / cloud model workers
```

Later target:

```text
operator CLI
    |
    v
remote always-on ARGUS runtime
    |
    +--> worker backends
    +--> external systems
    +--> durable state
```

The CLI should remain a control surface, not the place where mission durability lives.

## Architecture rule for future contributions

Before adding a capability to ARGUS, ask:

> Would this capability still make sense if the first consumer were not Digital Assets Lab?

If the answer is no, it probably belongs in the consumer application.
