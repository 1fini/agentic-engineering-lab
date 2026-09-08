# Agentic Engineering Lab

**Codename: ARGUS**

Agentic Engineering Lab is an open engineering project for building durable, observable, and controllable runtimes for long-running AI agent missions.

ARGUS is the runtime developed in this repository. It is intentionally not an "autonomous agent that does everything." ARGUS is a deterministic control plane that coordinates bounded workers, persists execution state, survives failures, and keeps humans above the loop rather than inside every routine step.

## Why this project exists

Most agent workflows are still short-lived and session-bound:

```text
human -> prompt -> agent -> result
```

That model breaks down when a mission must continue for hours or days, wait for future conditions, call external workers, recover after process death, classify failures, preserve evidence, cross external side-effect boundaries safely, and continue without repeated human approval.

ARGUS targets a different execution model:

```text
human intent
    |
    v
ARGUS deterministic control plane
    |
    +--> persist mission/step state
    +--> persist due_at eligibility
    +--> invoke bounded workers
    +--> validate structured outputs
    +--> persist attempts and usage
    +--> retry under explicit policy
    +--> persist effect intent before external calls
    +--> reconcile ambiguous external outcomes before replay
    +--> fail closed on uncertainty
    |
    v
mission continues until its contract is satisfied
```

## Core principles

1. **The orchestrator is not the LLM.** Durable control flow, retries, budgets, and recovery belong in deterministic code.
2. **LLMs are bounded workers, not the source of truth.** Worker input/output crosses explicit versioned contracts.
3. **Durability before autonomy.** Meaningful state must survive process death and restart.
4. **Human-over-the-loop, not human-in-the-loop.** Routine reversible work should not need approval.
5. **No blind retries around ambiguous effects.** Unknown outcomes fail closed until they can be reconciled safely.
6. **Observability is part of correctness.** State, attempts, timing, outcomes, effect lineage, and evidence must be inspectable.
7. **Build from real workloads, not speculative abstractions.** Generic capabilities are added only when a real consumer demonstrates the need.

## First reference workload

The first reference workload is the **Autonomous Editorial Learning Loop** from Digital Assets Lab.

Digital Assets Lab owns all domain logic: YouTube, Shorts, editorial policy, performance interpretation, generation semantics, quality checks, publication execution, and platform-specific reconciliation.

ARGUS owns only reusable execution primitives: durable steps, scheduling, bounded worker invocation, structured-result validation, retries, timeouts, attempt evidence, side-effect safety, recovery, budgets, and operational controls.

See [Architecture](docs/ARCHITECTURE.md) and [Reference Workload](docs/REFERENCE_WORKLOAD.md).

## Current implementation — Phase 3 complete

ARGUS now ships a local deterministic single-process runtime with three completed capability phases.

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

- persisted UTC `due_at` scheduling;
- deterministic dependency-aware due polling;
- restart-safe wakeup eligibility;
- strict versioned worker JSON contracts;
- bounded subprocess transport and POSIX process-tree timeout handling;
- OpenCode-compatible adapter isolated from orchestration semantics;
- deterministic retry/permanent/timeout/malformed classifications;
- durable worker attempt history and attempt limits;
- duration, exit code, optional token usage, and optional cost accounting;
- fail-closed behavior for ambiguous started attempts;
- integrated Phase 2 runtime with real subprocess acceptance.

### Phase 3 — Side-Effect Safety & Recovery

Delivered by Mission #21:

- versioned durable `EffectIntent` committed before any external call;
- stable parent effect correlation identity across restart;
- explicit `INTENT_COMMITTED -> OUTCOME_UNKNOWN -> CONFIRMED_*` lifecycle;
- durable execution and reconciliation receipts;
- dedicated effect-attempt history separate from Phase 2 worker attempts;
- versioned per-attempt identity while preserving the parent effect lineage;
- generic consumer-owned `EffectExecutor` and `EffectReconciler` boundaries;
- typed reconciliation decisions: confirmed applied, confirmed not applied, or still unknown;
- **no blind replay** while an external outcome is ambiguous;
- deterministic re-attempt only after durable confirmation that the previous effect did not apply;
- effect replay limits and duplicate-attempt prevention in the local single-process model;
- machine-readable effect state, receipt source/outcome, attempt lineage, and correlation evidence through `status` / `inspect` without raw consumer payloads;
- real subprocess crash acceptance using a persistent fake external system separate from ARGUS SQLite.

The Phase 3 acceptance proves crash safety at the important boundaries: after intent/before call, after remote acceptance/before local receipt, repeated unknown reconciliation, confirmed-not-applied re-attempt, and after receipt commit/before lineage synchronization.

ARGUS does **not** claim exactly-once effects for arbitrary remote systems. It guarantees that ambiguity is examined before replay: confirmed applied forbids replay, confirmed not applied may authorize a bounded re-attempt, and unknown remains fail-closed.

Canonical contracts are documented in:

- [Durable State](docs/DURABLE_STATE.md)
- [Runner and CLI](docs/RUNNER.md)
- [Workers](docs/WORKERS.md)
- [Attempts](docs/ATTEMPTS.md)
- [External Effects](docs/EFFECTS.md)
- [Restart and Recovery](docs/RECOVERY.md)
- [Architecture](docs/ARCHITECTURE.md)

A generic DAL-shaped reference workload remains domain-opaque. ARGUS contains no YouTube/Short/editorial semantics.

## Current operator surface

The CLI exposes machine-readable local state and scheduling/effect evidence:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db <mission-id>
argus inspect --store .argus/state.db <mission-id>
argus schedule --store .argus/state.db --due-at <timestamp> <mission-id> <step-id>
argus due --store .argus/state.db --at <timestamp>
```

The production worker/effect APIs live behind generic Python contracts. OpenCode and consumer-specific remote APIs are adapters; they are not orchestration authorities.

## Next — Phase 4: Operational Guardrails

The next reference-workload need is governable unattended execution.

Expected capabilities, only as demonstrated by the DAL integration, include:

- mission and attempt budgets;
- spend/cost budgets when observable;
- durable reservations where needed to prevent overspend at crash boundaries;
- pause / resume / cancel controls;
- workload-level and global kill switches;
- policy versioning;
- structured audit decisions for denied or halted execution;
- acceptance proving that no worker or external effect crosses a paused, cancelled, killed, or exhausted-budget boundary.

Later phases may add an always-on remote runtime and multi-workload maturity only when real consumers require them.

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

**Phase 3 — Side-Effect Safety & Recovery: complete.**

Missions #1, #10, and #21 establish durable restart, future scheduling and bounded workers, then external-effect intent/receipt/reconciliation with fail-closed replay semantics. Phase 4 is the next active capability target: deterministic budgets and operator controls for continuous unattended execution.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
