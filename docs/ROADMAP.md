# ARGUS Roadmap

This roadmap is capability-driven rather than feature-maximalist. ARGUS grows only when a real workload demonstrates a reusable execution need.

## Phase 0 — Foundation — COMPLETE

Goal: establish the public project, architecture boundary, and first reference workload.

- [x] Public repository created.
- [x] ARGUS codename and runtime role defined.
- [x] DAL / ARGUS ownership boundary documented.
- [x] First reference workload documented.
- [x] Initial technology choices recorded as ADRs.

Exit criterion: contributors can explain what belongs in ARGUS and what does not.

## Phase 1 — Durable Single-Process Runtime — COMPLETE

Delivered by Mission #1.

Goal: make one long-running mission survive process restarts without repeating completed work.

- [x] durable SQLite mission store;
- [x] ordered durable step journal;
- [x] transactional state transitions and audit entries;
- [x] versioned generic step contracts;
- [x] typed step results;
- [x] deterministic idempotency keys;
- [x] machine-readable `run`, `status`, `inspect`;
- [x] restart recovery that skips durably completed steps;
- [x] fail-closed behavior for ambiguous `RUNNING` work;
- [x] real subprocess failure-injection tests;
- [x] generic reference-workload fixture with no consumer business semantics.

Exit criterion: a real consumer-shaped fixture can kill ARGUS mid-run, restart it, and continue without rerunning an already committed step.

## Phase 2 — Durable Scheduling & Bounded Workers — COMPLETE

Delivered by Mission #10.

Goal: support missions that alternate between future wakeups and bounded external worker execution while preserving deterministic retry and restart semantics.

- [x] durable UTC `due_at` scheduling;
- [x] dependency-aware local due poller;
- [x] restart-safe due discovery;
- [x] duplicate read-only polling without duplicate attempts;
- [x] strict versioned worker request/response protocol;
- [x] generic bounded subprocess transport;
- [x] OpenCode-compatible adapter isolated from control flow;
- [x] structured output validation;
- [x] process-tree timeout handling;
- [x] retryable / permanent / timeout / malformed classification;
- [x] durable attempt records created before worker invocation;
- [x] deterministic attempt limits and retry timing;
- [x] duration, exit code, token and optional cost accounting;
- [x] fail-closed replay blocking for ambiguous `STARTED` attempts;
- [x] integrated `Phase2Runtime` composing scheduler + attempts + workers;
- [x] safe mission completion reconciliation after a final committed step;
- [x] real subprocess acceptance for restart, retry, timeout, malformed output, permanent failure and process death;
- [x] generic DAL-shaped fixture remains domain-opaque.

Exit criterion: a consumer can persist future work, restart the runtime, wake only at the due boundary, execute a bounded worker, classify failures, retry according to deterministic policy, and inspect durable attempt evidence. Ambiguous executions remain blocked rather than blindly replayed.

## Phase 3 — Side-Effect Safety and Recovery — NEXT

Goal: safely orchestrate external operations whose outcome may be ambiguous after failure.

Candidate capabilities, added only when exercised by the reference workload:

- durable side-effect intent;
- execution receipt contract;
- explicit `UNKNOWN_EFFECT` / reconciliation state or protocol;
- generic consumer-owned reconciliation hook;
- duplicate-effect prevention;
- correlation and artifact lineage;
- crash-boundary acceptance harness covering intent / call / receipt / reconciliation boundaries.

Exit criterion: a consumer can prove that a crash at each relevant external-effect boundary cannot cause an unexamined duplicate effect.

## Phase 4 — Operational Guardrails

Goal: make continuous autonomy governable.

Candidate capabilities:

- mission budgets;
- attempt budgets;
- spend budgets when observable;
- pause / resume;
- cancellation;
- workload kill switch;
- global kill switch;
- policy versioning;
- structured audit events.

Exit criterion: operators can bound and stop autonomous execution deterministically without relying on model cooperation.

## Phase 5 — Remote Always-On Runtime

Goal: move durability away from the operator laptop while preserving mission semantics.

Candidate capabilities:

- daemon / service mode;
- remote CLI transport;
- secure runtime configuration;
- worker backend configuration;
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

Before promoting an item onto the active roadmap, answer:

1. Which real workload needs it now?
2. Why cannot that concern remain inside the consumer?
3. What is the smallest reusable primitive that solves the demonstrated problem?

If those questions do not have concrete answers, the item stays out of the implementation plan.
