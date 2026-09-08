# Agentic Engineering Lab

**Codename: ARGUS**

Agentic Engineering Lab is an open engineering project for building durable, observable, and controllable runtimes for long-running AI agent missions.

ARGUS is the runtime developed in this repository. It is intentionally not an "autonomous agent that does everything." ARGUS is a deterministic control plane that coordinates bounded workers, persists execution state, survives failures, and keeps humans above the loop rather than inside every routine step.

## Why this project exists

Most agent workflows are still short-lived and session-bound:

```text
human -> prompt -> agent -> result
```

That model breaks down when a mission must continue for hours or days, wait for future conditions, call external workers, recover after process death, classify failures, preserve evidence, and continue without repeated human approval.

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
    +--> fail closed on ambiguity
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
6. **Observability is part of correctness.** State, attempts, timing, outcomes, and evidence must be inspectable.
7. **Build from real workloads, not speculative abstractions.** Generic capabilities are added only when a real consumer demonstrates the need.

## First reference workload

The first reference workload is the **Autonomous Editorial Learning Loop** from Digital Assets Lab.

Digital Assets Lab owns all domain logic: YouTube, Shorts, editorial policy, performance interpretation, generation semantics, quality checks, and publication rules.

ARGUS owns only reusable execution primitives: durable steps, scheduling, bounded worker invocation, structured-result validation, retries, timeouts, attempt evidence, recovery, budgets, and operational controls.

See [Architecture](docs/ARCHITECTURE.md) and [Reference Workload](docs/REFERENCE_WORKLOAD.md).

## Current implementation — Phase 2 complete

ARGUS now ships a local deterministic single-process runtime with two completed capability phases.

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

- persisted UTC `due_at` scheduling in the same SQLite state file;
- deterministic due polling with dependency ordering;
- restart-safe wakeup eligibility;
- strict versioned `WorkerRequest` / `WorkerResponse` JSON contracts;
- bounded subprocess transport;
- process-group timeout termination on POSIX;
- bounded stdout/stderr retention;
- OpenCode-compatible `opencode run` adapter isolated from orchestration semantics;
- strict structured-output validation;
- explicit success / retryable / permanent / timeout / malformed classifications;
- durable attempt history before every external worker invocation;
- deterministic attempt limits and retry delays;
- duration, exit code, optional token usage, and optional cost accounting;
- fail-closed behavior for a durable `STARTED` attempt with no known outcome;
- `Phase2Runtime` composing scheduler + attempts + bounded workers;
- safe mission-completion reconciliation after a final committed step;
- real subprocess acceptance covering restart, retry, malformed output, timeout, attempt exhaustion, and ambiguous process death.

Canonical contracts are documented in:

- [Durable State](docs/DURABLE_STATE.md)
- [Runner and CLI](docs/RUNNER.md)
- [Workers](docs/WORKERS.md)
- [Attempts](docs/ATTEMPTS.md)
- [Restart and Recovery](docs/RECOVERY.md)
- [Architecture](docs/ARCHITECTURE.md)

A generic DAL-shaped reference manifest is available at `examples/reference-workload-v1.json`. ARGUS treats its payload as opaque and contains no YouTube/Short/editorial semantics.

## Current operator surface

The CLI exposes machine-readable local state and scheduling evidence:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db <mission-id>
argus inspect --store .argus/state.db <mission-id>
argus schedule --store .argus/state.db --due-at <timestamp> <mission-id> <step-id>
argus due --store .argus/state.db --at <timestamp>
```

The production bounded-worker APIs live behind generic Python contracts. OpenCode is one adapter; it is not the orchestration authority.

## What Phase 2 does **not** claim

Phase 2 does not provide exactly-once external side effects.

If ARGUS has durable evidence that an attempt started but no trustworthy outcome was committed, automatic replay is blocked. Likewise, if process-tree termination cannot be established, the attempt is treated as ambiguous.

Those cases require the next capability phase: explicit side-effect intent, receipt, and reconciliation.

## Next — Phase 3: Side-Effect Safety and Recovery

The next reference-workload need is safe execution of external operations whose outcome can be ambiguous after failure.

Expected capabilities, only as demonstrated by the DAL integration, include:

- durable side-effect intent;
- execution receipt contract;
- explicit unknown-effect state/protocol;
- generic reconciliation hook;
- duplicate-effect prevention;
- correlation and artifact lineage;
- crash-boundary acceptance tests.

Later phases will add operational budgets, pause/resume/cancel/kill-switch controls, and an always-on remote runtime.

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

**Phase 2 — Durable Scheduling & Bounded Workers: complete.**

Mission #1 established durability. Mission #10 adds future wakeups, bounded OpenCode-compatible worker execution, strict worker contracts, durable attempts, retries, timeout handling, and usage evidence. Phase 3 will address ambiguous external side effects rather than weakening the fail-closed guarantees established here.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
