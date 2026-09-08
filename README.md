# Agentic Engineering Lab

**Codename: ARGUS**

Agentic Engineering Lab is an open engineering project for building durable, observable, and controllable runtimes for long-running AI agent missions.

ARGUS is the runtime developed in this repository. It is intentionally not an "autonomous agent that does everything." ARGUS is a deterministic control plane that coordinates bounded workers, persists execution state, survives failures, governs external effects, and keeps humans above the loop rather than inside every routine step.

## Why this project exists

Most agent workflows are still short-lived and session-bound:

```text
human -> prompt -> agent -> result
```

That model breaks down when a mission must continue for hours or days, wait for future conditions, call external workers, recover after process death, preserve evidence, cross external side-effect boundaries safely, respect operator controls and budgets, and continue without repeated human approval.

ARGUS targets a different execution model:

```text
human intent
    |
    v
ARGUS deterministic control plane
    |
    +--> persist mission/step state
    +--> persist due_at eligibility
    +--> gate execution on durable controls and budgets
    +--> reserve spend before cost-bearing calls
    +--> invoke bounded workers
    +--> validate structured outputs
    +--> persist attempts and usage
    +--> retry under explicit policy
    +--> persist effect intent before external calls
    +--> reconcile ambiguous external outcomes before replay
    +--> fail closed on uncertainty
    |
    v
mission continues until its contract is satisfied or a durable guardrail stops it
```

## Core principles

1. **The orchestrator is not the LLM.** Durable control flow, retries, budgets, recovery, and operator controls belong in deterministic code.
2. **LLMs are bounded workers, not the source of truth.** Worker input/output crosses explicit versioned contracts.
3. **Durability before autonomy.** Meaningful state must survive process death and restart.
4. **Human-over-the-loop, not human-in-the-loop.** Routine reversible work should not need approval.
5. **No blind retries around ambiguous effects.** Unknown outcomes fail closed until they can be reconciled safely.
6. **Controls are code, not prompts.** Pause, cancel, kill switches, and budgets cannot be overridden by model cooperation.
7. **Observability is part of correctness.** State, attempts, timing, outcomes, effect lineage, controls, budgets, and policy provenance must be inspectable.
8. **Build from real workloads, not speculative abstractions.** Generic capabilities are added only when a real consumer demonstrates the need.

## First reference workload

The first reference workload is the **Autonomous Editorial Learning Loop** from Digital Assets Lab.

Digital Assets Lab owns all domain logic: YouTube, Shorts, editorial policy, performance interpretation, generation semantics, quality checks, publication execution, platform-specific reconciliation, and business-specific guardrail values.

ARGUS owns only reusable execution primitives: durable steps, scheduling, bounded worker invocation, structured-result validation, retries, timeouts, attempt evidence, side-effect safety, recovery, budgets, operator controls, and execution gates.

See [Architecture](docs/ARCHITECTURE.md) and [Reference Workload](docs/REFERENCE_WORKLOAD.md).

## Current implementation — Phase 4 complete

ARGUS now ships a local deterministic single-process runtime with four completed capability phases.

### Phase 1 — Durable Single-Process Runtime

Delivered by Mission #1:

- Python 3.12 package and `argus` CLI;
- versioned generic mission/step contracts;
- SQLite durable mission and ordered step state;
- transactional state transitions + append-only journal;
- deterministic idempotency keys;
- deterministic callable runner;
- machine-readable `run`, `status`, and `inspect`;
- restart behavior that skips durably completed steps;
- fail-closed handling of ambiguous work after process death;
- real subprocess crash/restart tests.

### Phase 2 — Durable Scheduling & Bounded Workers

Delivered by Mission #10:

- persisted UTC `due_at` scheduling and dependency-aware polling;
- restart-safe wakeup eligibility;
- strict versioned worker JSON contracts;
- bounded subprocess transport and POSIX process-tree timeout handling;
- OpenCode-compatible adapter isolated from orchestration semantics;
- deterministic retry/permanent/timeout/malformed classifications;
- durable worker attempt history and attempt limits;
- duration, exit code, token usage, and optional cost accounting;
- fail-closed behavior for ambiguous started attempts;
- real subprocess acceptance across restart, retry, timeout, malformed output, and process death.

### Phase 3 — Side-Effect Safety & Recovery

Delivered by Mission #21:

- versioned durable `EffectIntent` committed before any external call;
- stable parent effect correlation identity across restart;
- explicit `INTENT_COMMITTED -> OUTCOME_UNKNOWN -> CONFIRMED_*` lifecycle;
- durable execution and reconciliation receipts;
- dedicated effect-attempt lineage separate from Phase 2 worker attempts;
- generic consumer-owned `EffectExecutor` and `EffectReconciler` boundaries;
- typed reconciliation decisions: confirmed applied, confirmed not applied, or still unknown;
- no blind replay while an external outcome is ambiguous;
- deterministic re-attempt only after durable confirmation that the previous effect did not apply;
- real subprocess crash acceptance using a persistent fake external system separate from ARGUS SQLite.

ARGUS does **not** claim universal exactly-once effects. It guarantees examined replay: ambiguity must be resolved before another external call can be authorized.

### Phase 4 — Operational Guardrails & Execution Gates

Delivered by Mission #30:

- versioned durable `GuardrailPolicy` with policy hash/provenance;
- durable mission control state: `ACTIVE`, `PAUSED`, `CANCELLED`;
- terminal cancellation semantics;
- durable workload-scoped and global kill switches;
- deterministic control precedence: cancelled -> global kill -> workload kill -> paused -> allow;
- versioned `BudgetPolicy` with independent worker-attempt, effect-attempt, and optional spend limits;
- authoritative attempt consumption derived from Phase 2/3 attempt evidence rather than duplicate counters;
- crash-safe spend ledger: `RESERVED -> COMMITTED | RELEASED`;
- outstanding reservations continue to consume capacity across restart and are never automatically released on ambiguity;
- `GovernedRuntime` as the authoritative unattended execution surface;
- control and budget gates immediately before worker/effect allocation;
- deterministic spend reservation before cost-bearing invocation;
- trusted worker cost commits actual spend; unknown cost remains reserved;
- effect cost may be committed through a consumer-provided generic resolver when trustworthy evidence exists;
- structured governed-execution audit with guardrail and budget policy hashes;
- real subprocess acceptance for pause/resume/cancel, kill switches, budget exhaustion, reservation crashes, worker ambiguity, and Phase 3 effect ambiguity;
- restart-safe reuse of the same deterministic reservation when ARGUS dies after reservation but before attempt allocation.

The final Phase 4 acceptance caught and fixed a real recovery bug: restart originally re-checked fresh spend capacity before recognizing its already-durable reservation. `GovernedRuntime` now resolves the deterministic reservation identity first, so the existing reservation is reused idempotently rather than self-denying as exhausted budget.

Canonical contracts are documented in:

- [Durable State](docs/DURABLE_STATE.md)
- [Runner and CLI](docs/RUNNER.md)
- [Workers](docs/WORKERS.md)
- [Attempts](docs/ATTEMPTS.md)
- [External Effects](docs/EFFECTS.md)
- [Guardrails](docs/GUARDRAILS.md)
- [Budgets](docs/BUDGETS.md)
- [Governed Execution](docs/GOVERNED_EXECUTION.md)
- [Restart and Recovery](docs/RECOVERY.md)
- [Architecture](docs/ARCHITECTURE.md)

ARGUS remains domain-opaque. It contains no YouTube, Short, retention, hook, editorial, or platform publication semantics.

## Current operator surface

The CLI exposes machine-readable runtime and operator state, including commands such as:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db <mission-id>
argus inspect --store .argus/state.db <mission-id>
argus schedule --store .argus/state.db --due-at <timestamp> <mission-id> <step-id>
argus due --store .argus/state.db --at <timestamp>
argus guardrail-init --store .argus/state.db --workload-scope <scope> <mission-id>
argus control --store .argus/state.db pause <mission-id>
argus control --store .argus/state.db resume <mission-id>
argus control --store .argus/state.db cancel <mission-id>
argus kill-switch --store .argus/state.db --workload-scope <scope> on
argus kill-switch --store .argus/state.db --global-scope on
argus budget-init --store .argus/state.db <mission-id> [...limits...]
```

The production worker/effect APIs live behind generic Python contracts. OpenCode and consumer-specific remote APIs are adapters; they are not orchestration authorities.

## Next — Phase 5: Remote Always-On Runtime

The next generic runtime phase will move execution away from an operator laptop while preserving the same mission semantics.

Candidate capabilities, added only when the active workload requires them, include:

- daemon / service mode;
- remote CLI transport;
- secure runtime configuration;
- worker backend configuration;
- health and liveness reporting;
- deployment documentation;
- backup / restore of runtime state.

Before expanding the generic runtime further, the first reference workload should consume the completed Phase 1–4 contracts end to end. Remote deployment must preserve, not replace, the durability and guardrail semantics already proven locally.

See the [Roadmap](docs/ROADMAP.md).

## Non-goals

ARGUS is not intended to become:

- a domain-specific media automation framework;
- a hidden prompt chain with no durable state;
- an unbounded self-modifying agent;
- a generic workflow DSL before real workloads require one;
- a system that silently changes product, editorial, security, or safety policy;
- a reason to move business logic out of the applications that own it.

## Development strategy

ARGUS follows a vertical-slice approach:

1. start from a real consumer requirement;
2. define the smallest generic execution primitive that satisfies it;
3. implement it with durable state and focused tests;
4. prove it at process/failure boundaries;
5. integrate it with the consumer;
6. generalize only after repeated evidence.

See [Development](docs/DEVELOPMENT.md).

## Security

This is a public repository. Never commit API keys, OAuth tokens, cookies, credentials, private prompts, private datasets, internal URLs, or secrets from consumer projects.

See [SECURITY.md](SECURITY.md).

## Status

**Phase 4 — Operational Guardrails & Execution Gates: complete.**

Missions #1, #10, #21, and #30 establish durable restart, future scheduling and bounded workers, safe external-effect reconciliation, and deterministic operator/budget gates for unattended execution. The next product milestone is concrete Digital Assets Lab integration; Phase 5 will address always-on remote operation when that real workload demonstrates the need.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
