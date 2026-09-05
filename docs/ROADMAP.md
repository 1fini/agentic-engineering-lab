# ARGUS Roadmap

This roadmap is intentionally capability-driven rather than feature-maximalist.

ARGUS should grow only when a real workload demonstrates a generic execution need.

## Phase 0 — Foundation

Goal: establish the public project, architecture boundary, and first reference workload.

- [x] Public repository created.
- [x] ARGUS codename and runtime role defined.
- [x] DAL / ARGUS ownership boundary documented.
- [x] First reference workload documented.
- [ ] Initial implementation issue created from the current DAL integration need.
- [ ] Initial technology choices recorded as ADRs.

Exit criterion: contributors can explain what belongs in ARGUS and what does not.

## Phase 1 — Durable Single-Process Runtime

Goal: make one long-running mission survive process restarts without repeating completed work.

Candidate capabilities:

- durable mission store;
- durable step journal;
- transactional state transitions;
- versioned step contracts;
- typed step results;
- basic CLI for run / status / inspect;
- restart recovery;
- deterministic idempotency keys;
- focused failure-injection tests.

Explicitly out of scope:

- distributed execution;
- multi-node coordination;
- generic workflow DSL;
- dynamic agent swarms;
- web dashboard.

Exit criterion: a real consumer can execute a multi-step mission, kill ARGUS mid-run, restart it, and continue without rerunning an already committed step.

## Phase 2 — Waiting, Scheduling, and Bounded Workers

Goal: support missions that alternate between active work and future wakeups.

Candidate capabilities:

- durable `due_at` scheduling;
- waiting state;
- local scheduler loop;
- OpenCode worker adapter;
- structured-output schema validation;
- process-tree timeout;
- retryable / permanent error classification;
- attempt limits;
- basic duration and cost accounting.

Exit criterion: a consumer can persist a future wakeup, restart the runtime, and receive the step when it becomes due; worker failures are classified rather than treated as opaque crashes.

## Phase 3 — Side-Effect Safety and Recovery

Goal: safely orchestrate external operations whose outcome may be ambiguous after failure.

Candidate capabilities:

- durable side-effect intent;
- execution receipt contract;
- `UNKNOWN_EFFECT` state;
- generic reconciliation hook;
- duplicate-effect prevention;
- correlation and artifact lineage;
- crash-boundary test harness.

Exit criterion: a consumer can prove that a crash at each relevant boundary cannot cause an unexamined duplicate external effect.

## Phase 4 — Operational Guardrails

Goal: make continuous autonomy governable.

Candidate capabilities:

- mission budgets;
- attempt budgets;
- cost budgets when observable;
- pause / resume;
- cancellation;
- workload kill switch;
- global kill switch;
- policy versioning;
- structured audit events.

Exit criterion: operators can bound and stop autonomous execution deterministically without relying on model cooperation.

## Phase 5 — Remote Always-On Runtime

Goal: move durability away from the operator laptop while preserving the same mission semantics.

Candidate capabilities:

- daemon / service mode;
- remote CLI transport;
- secure runtime configuration;
- worker backend abstraction;
- health and liveness reporting;
- deployment documentation;
- backup / restore of runtime state.

Exit criterion: closing the operator laptop does not interrupt long-running missions.

## Phase 6 — Multi-Workload Maturity

Goal: validate ARGUS as a reusable runtime with more than one materially different consumer.

Possible later concerns, only if demonstrated:

- concurrency controls;
- worker pools;
- richer dependency graphs;
- multiple model backends;
- remote artifact stores;
- event-driven wakeups;
- operator UI;
- distributed execution.

None of these should be implemented only because they are common in workflow engines.

## Decision rule

Before promoting an item onto the active roadmap, answer three questions:

1. Which real workload needs it now?
2. Why cannot that concern remain inside the consumer?
3. What is the smallest reusable primitive that solves the demonstrated problem?

If those questions do not have concrete answers, the item stays out of the implementation plan.
