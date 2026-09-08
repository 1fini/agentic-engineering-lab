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

## Phase 3 — Side-Effect Safety and Recovery — COMPLETE

Delivered by Mission #21.

Goal: safely orchestrate external operations whose outcome may be ambiguous after failure.

- [x] durable versioned external-effect intent before external execution;
- [x] stable parent effect correlation identity;
- [x] explicit durable unknown-outcome state;
- [x] versioned execution/reconciliation receipts;
- [x] generic consumer-owned executor and reconciler interfaces;
- [x] typed APPLIED / NOT_APPLIED / UNKNOWN reconciliation decisions;
- [x] dedicated effect attempts separate from Phase 2 worker attempts;
- [x] stable per-attempt identity and effect lineage;
- [x] replay forbidden while the external outcome is unknown;
- [x] deterministic re-attempt only after confirmed non-application;
- [x] bounded effect-attempt policy and duplicate-attempt prevention;
- [x] machine-readable effect/reconciliation lineage without raw consumer payloads;
- [x] real subprocess crash matrix using a fake external system persisted separately from ARGUS state;
- [x] generic DAL-shaped effect fixture remains domain-opaque.

Exit criterion: a consumer can prove that crashes before call, after remote acceptance/before local receipt, during reconciliation, and after receipt commit cannot cause an unexamined duplicate effect. Confirmed applied forbids replay; confirmed not applied may authorize a bounded re-attempt; unknown remains fail-closed.

## Phase 4 — Operational Guardrails & Execution Gates — COMPLETE

Delivered by Mission #30.

Goal: make continuous autonomy governable without relying on model cooperation.

- [x] versioned durable mission guardrail policy with policy hash/provenance;
- [x] durable `ACTIVE / PAUSED / CANCELLED` control state;
- [x] terminal cancellation;
- [x] durable workload-scoped kill switch;
- [x] durable global kill switch;
- [x] deterministic control gate precedence;
- [x] worker-attempt budget derived from authoritative Phase 2 attempt evidence;
- [x] effect-attempt budget derived from authoritative Phase 3 attempt evidence;
- [x] optional spend/cost budget;
- [x] crash-safe `RESERVED -> COMMITTED | RELEASED` spend ledger;
- [x] outstanding reservations count against available capacity across restart;
- [x] no automatic release of ambiguous reservations;
- [x] deterministic reservation identity for the next worker/effect attempt;
- [x] restart-safe idempotent reservation reuse after crash-before-attempt;
- [x] `GovernedRuntime` as the authoritative continuous unattended execution surface;
- [x] control/budget gates before Phase 2 worker allocation;
- [x] control/budget gates before Phase 3 effect allocation and re-attempt;
- [x] trusted worker cost settlement and generic effect cost resolver boundary;
- [x] structured governed execution audit with guardrail + budget policy hashes;
- [x] machine-readable guardrail, budget, reservation, and policy evidence;
- [x] real subprocess acceptance for pause/resume/cancel and kill switches across restart;
- [x] real subprocess acceptance for worker/effect attempt exhaustion before invocation;
- [x] real `os._exit()` acceptance around spend reservation -> attempt -> worker boundaries;
- [x] real `os._exit()` acceptance around spend reservation -> effect -> remote acceptance -> reconciliation boundaries;
- [x] generic DAL-shaped governance fixture remains domain-opaque.

The final acceptance caught and fixed a real crash-recovery bug: restart after reservation/before attempt originally performed a fresh spend check and could deny itself because its own durable reservation already consumed capacity. The delivered runtime resolves the deterministic reservation identity first and reuses the same reservation idempotently.

Exit criterion: operators can bound, pause, resume, cancel, and stop autonomous execution deterministically, and no governed worker/effect call can cross a durable paused/cancelled/killed or exhausted-budget boundary. Crash/restart cannot silently duplicate reservations or bypass Phase 2/3 ambiguity handling.

## Reference-workload integration — ACTIVE PRODUCT MILESTONE

Before expanding the generic runtime merely because another platform feature is possible, the Digital Assets Lab reference workload should consume Phase 1–4 end to end.

Concrete integration work includes:

- mapping DAL workflow operations to generic ARGUS mission/step contracts;
- supplying DAL worker prompts and structured schemas behind the generic worker boundary;
- connecting analytics, diagnosis, hypothesis, generation, and QA to ARGUS scheduling/attempt semantics;
- implementing DAL-owned upload execution and remote reconciliation behind generic Phase 3 effect contracts;
- supplying DAL-specific guardrail/budget values behind generic Phase 4 policies;
- proving the real cross-repository learning loop with public execution controls.

This integration remains a DAL responsibility; ARGUS must not absorb YouTube/editorial semantics.

## Phase 5 — Remote Always-On Runtime — NEXT GENERIC PHASE

Goal: move durability away from the operator laptop while preserving Phase 1–4 mission semantics.

Candidate capabilities, added when the reference workload demonstrates the need:

- daemon / service mode;
- remote CLI transport;
- secure runtime configuration;
- worker backend configuration;
- health and liveness reporting;
- deployment documentation;
- backup / restore of runtime state.

Exit criterion: closing the operator laptop does not interrupt long-running missions, and service/process recovery preserves the same scheduling, worker, effect, guardrail, and budget invariants already proven locally.

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
