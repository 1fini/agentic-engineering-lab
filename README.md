# Agentic Engineering Lab

**Codename: ARGUS**

Agentic Engineering Lab is an open engineering project for building durable, observable, and controllable runtimes for long-running AI agent missions.

ARGUS is the codename of the runtime developed in this repository.

The project is intentionally not an "autonomous agent that does everything." ARGUS is a control plane that coordinates specialized workers, persists execution state, enforces policies and budgets, survives failures, and keeps humans above the loop rather than inside every step.

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
   Every mission should preserve enough evidence to explain what happened, why, with which inputs, outputs, costs, and policies.

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

## Target capabilities

ARGUS is expected to grow toward:

- durable mission and step state;
- resumable execution;
- due-at scheduling and wakeups;
- bounded OpenCode / model worker execution;
- versioned structured input/output contracts;
- retry, timeout, and error classification;
- idempotency and ambiguous-side-effect reconciliation;
- correlated logs, events, artifacts, and evidence;
- cost, time, and attempt budgets;
- pause / resume / cancel / kill-switch controls;
- policy-aware execution;
- local-first operation with a path to remote always-on execution;
- CLI-based mission supervision.

## Non-goals

ARGUS is not intended to become:

- a domain-specific YouTube or media automation framework;
- a hidden prompt chain with no durable state;
- an unbounded self-modifying agent;
- a generic workflow DSL before real workloads require one;
- a system that silently changes product, editorial, security, or safety policy;
- a reason to move business logic out of the applications that own it.

## Conceptual architecture

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

## CLI direction

The exact interface is not frozen, but the intended operator experience is along these lines:

```bash
argus mission run <mission>
argus mission status <mission-id>
argus mission pause <mission-id>
argus mission resume <mission-id>
argus mission inspect <mission-id>
```

The runtime should eventually be able to run continuously on an always-on host while the CLI acts as a remote control plane.

## Development strategy

ARGUS follows a vertical-slice approach:

1. start from a real consumer requirement;
2. define the smallest generic execution primitive that satisfies it;
3. implement it with durable state and focused tests;
4. integrate it with the consumer;
5. observe failure modes;
6. generalize only after repeated evidence.

See the [Roadmap](docs/ROADMAP.md).

## Security

This is a public repository. Never commit API keys, OAuth tokens, cookies, credentials, private prompts, private datasets, internal URLs, or secrets from consumer projects.

See [SECURITY.md](SECURITY.md).

## Status

**Early foundation / architecture phase.**

The first implementation milestone is to provide the smallest durable execution slice required by the Digital Assets Lab reference workload.

## License

No open-source license has been selected yet. Until a license is added, normal copyright rules apply.
