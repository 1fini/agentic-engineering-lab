# Agentic Engineering Lab

**Codename: ARGUS**

Agentic Engineering Lab is an open engineering project for building durable, observable, and controllable runtimes for long-running AI agent missions.

ARGUS is the codename of the runtime developed in this repository.

The project is intentionally not an "autonomous agent that does everything." ARGUS is a control plane that coordinates specialized workers, persists execution state, enforces execution semantics, survives failures, and keeps humans above the loop rather than inside every step.

## Why this project exists

Most agent workflows are still short-lived and session-bound:

```text
human -> prompt -> agent -> result
```

That model works well for bounded tasks, but breaks down when a mission must continue for hours or days, call multiple tools or models, wait for external events, recover from crashes, verify outputs, and make progress without repeated human approval.

ARGUS targets a different execution model:

```text
human intent
    |
    v
ARGUS control plane
    |
    +--> plan / schedule work
    +--> invoke bounded workers
    +--> validate structured outputs
    +--> persist state and evidence
    +--> retry or recover safely
    +--> enforce budgets and policies
    +--> pause on true exceptions
    |
    v
mission continues until its contract is satisfied
```

## Core principles

1. **The orchestrator is not the LLM.**
   Durable control flow, state, retries, budgets, and side-effect safety belong in deterministic code.

2. **LLMs are workers, not the source of truth.**
   They perform cognitive tasks behind explicit contracts and return validated structured outputs.

3. **Durability before autonomy.**
   A long-running mission must be resumable after process death, machine restart, provider failure, or partial side effects.

4. **Human-over-the-loop, not human-in-the-loop.**
   Routine steps should not require approval. Humans define policy and intervene when the system reaches an explicit exceptional state.

5. **No blind retries around side effects.**
   Unknown external outcomes must be reconciled before a side effect is attempted again.

6. **Observability is part of correctness.**
   Every mission should preserve enough evidence to explain what happened, why, with which inputs, outputs, and policies.

7. **Build from real workloads, not speculative abstractions.**
   Generic ARGUS capabilities are added only when exercised by a real consumer.

## First reference workload

The first reference workload is the **Autonomous Editorial Learning Loop** from Digital Assets Lab.

That workload needs to:

- observe real external metrics;
- compare evidence at equivalent maturity;
- produce a bounded hypothesis;
- generate and validate a new artifact;
- perform an external publication side effect safely;
- schedule future observations;
- resume after interruption without duplicating effects.

Digital Assets Lab owns all domain logic: YouTube, Shorts, editorial policy, performance interpretation, generation semantics, and publication rules.

ARGUS owns only reusable execution primitives: durable steps, scheduling, worker invocation, structured-result validation, retries, timeouts, budgets, observability, pause/resume, recovery, and kill switches.

See [Architecture](docs/ARCHITECTURE.md) and [Reference Workload](docs/REFERENCE_WORKLOAD.md).

## Current implementation — Phase 1

ARGUS now has a local deterministic single-process runtime foundation:

- Python 3.12 package and `argus` CLI;
- versioned generic mission/step contracts;
- SQLite durable mission and ordered step state;
- transactional state transitions + append-only journal;
- deterministic idempotency keys;
- explicit corruption/schema validation;
- deterministic callable worker registry;
- versioned JSON mission manifests;
- machine-readable `run`, `status`, and `inspect` commands;
- restart behavior that skips durably completed steps;
- fail-closed handling of a step left `RUNNING` by process death;
- real subprocess crash/restart tests proving completed steps are not executed twice.

The canonical Phase 1 contracts are documented in:

- [Durable State](docs/DURABLE_STATE.md)
- [Runner and CLI](docs/RUNNER.md)
- [Restart and Recovery](docs/RECOVERY.md)

A generic reference manifest is available at `examples/reference-workload-v1.json`.

## Not implemented yet

The following remain roadmap items, not current runtime claims:

- due-at scheduling and wakeups;
- OpenCode / model worker integration;
- retry and timeout classification;
- ambiguous external side-effect reconciliation;
- cost/time/attempt budgets;
- pause / resume / cancel / kill-switch controls;
- remote always-on service mode;
- distributed execution.

See the [Roadmap](docs/ROADMAP.md).

## Non-goals

ARGUS is not intended to become:

- a domain-specific YouTube or media automation framework;
- a hidden prompt chain with no durable state;
- an unbounded self-modifying agent;
- a generic workflow DSL before real workloads require one;
- a system that silently changes product, editorial, security, or safety policy;
- a reason to move business logic out of the applications that own it.

## Target architecture

The long-term direction remains:

```text
+---------------------------+
|        Human / CLI        |
|  run status pause resume  |
+-------------+-------------+
              |
              v
+---------------------------+
|       ARGUS Runtime       |
|---------------------------|
| mission state             |
| scheduler                 |
| policy / budgets          |
| retries / timeouts        |
| recovery                  |
| observability             |
+------+------+-------------+
       |      |
       |      +-------------------+
       v                          v
+-------------+          +------------------+
| AI Workers  |          | External Systems |
| OpenCode    |          | APIs / services  |
| LLMs/tools  |          | repos / queues   |
+-------------+          +------------------+
       |
       v
+---------------------------+
|  Structured step result   |
+---------------------------+
```

Phase 1 intentionally implements only the durable local subset needed to prove restart semantics.

## Current CLI

The current Phase 1 control surface is:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db <mission-id>
argus inspect --store .argus/state.db <mission-id>
```

`--fixture-workers` enables only explicit generic test/demo workers. It is not an OpenCode or production worker backend.

Future phases will extend the operator surface with scheduling and operational controls without moving durable control flow into prompts.

## Development strategy

ARGUS follows a vertical-slice approach:

1. start from a real consumer requirement;
2. define the smallest generic execution primitive that satisfies it;
3. implement it with durable state and focused tests;
4. integrate it with the consumer;
5. observe failure modes;
6. generalize only after repeated evidence.

See [Development](docs/DEVELOPMENT.md) for the local install/test path.

## Security

This is a public repository. Never commit API keys, OAuth tokens, cookies, credentials, private prompts, private datasets, internal URLs, or secrets from consumer projects.

See [SECURITY.md](SECURITY.md).

## Status

**Phase 1 — Durable Single-Process Runtime.**

Mission #1 establishes the first executable durability slice required by the Digital Assets Lab reference workload. Later ARGUS missions will add scheduling, bounded OpenCode execution, side-effect reconciliation, and operational guardrails only when exercised by the consumer.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
