# ADR-0001 — Phase 1 runtime stack

- **Status:** Accepted
- **Date:** 2026-09-08
- **Mission:** #1
- **Workstream:** #2

## Context

ARGUS Phase 1 must prove durable, restartable, single-process execution for a real consumer before the project grows into scheduling, model workers, side-effect reconciliation, or distributed execution.

The first reference workload, Digital Assets Lab, is already Python-based. The runtime needs a transactional local store that is inspectable, portable, dependency-light, and capable of surviving process termination.

## Decision

Use the following Phase 1 foundation:

- **Python 3.12+** for the runtime and CLI;
- a conventional **`src/` package layout**;
- **setuptools** as the minimal build backend;
- **pytest** for tests;
- the Python standard-library **`sqlite3`** module with a local SQLite database for durable mission/step state in WS2;
- **argparse** for the initial CLI, avoiding a CLI framework until complexity demonstrates a need;
- **GitHub Actions** for CI on Python 3.12.

No workflow framework, ORM, async runtime, broker, external database, or model SDK is introduced in Phase 1.

## Rationale

Python minimizes integration friction with the first consumer while keeping worker adapters straightforward later. SQLite provides atomic local transactions, crash-safe persistence under its documented guarantees, a single inspectable state file, and no operational service dependency. Standard-library primitives keep the first durability experiment easy to audit.

The choice is deliberately conservative. ARGUS is proving execution semantics, not framework sophistication.

## Rejected alternatives

### PostgreSQL

Strong persistence semantics, but it adds an external service before a single-process reference workload requires one. Reassess for remote/multi-host runtime requirements.

### JSON/filesystem journal

Easy to inspect, but transactional multi-record state changes, concurrent duplicate invocation protection, schema evolution, and corruption handling become bespoke concerns. SQLite gives a smaller and more rigorous persistence boundary.

### Existing workflow engines

Temporal, Prefect, Dagster and similar systems provide many capabilities ARGUS may eventually need, but adopting one now would obscure which semantics the project is actually trying to learn and would import abstractions not yet justified by the reference workload.

### ORM

Phase 1 has a very small schema. Direct SQL keeps transaction boundaries visible. Reassess if schema complexity becomes a maintenance problem.

### Typer/Click

Useful for richer CLIs, but `argparse` is sufficient for the Phase 1 commands and avoids another runtime dependency. Reassess when command composition or UX complexity justifies it.

## Consequences

- Phase 1 remains local and single-process.
- SQLite transaction boundaries become part of the runtime's correctness model and must be tested at crash/restart boundaries.
- Consumer payloads remain opaque data; SQLite schema must not encode Digital Assets Lab business concepts.
- The package exposes an `argus` console script but runtime commands are added only as their durable semantics are implemented.
- Later phases may replace or abstract persistence only with a migration/reassessment ADR and executable compatibility evidence.

## Reassessment triggers

Revisit this decision if one or more real workloads require:

- multiple runtime processes writing the same mission store;
- remote always-on execution with a managed database;
- state volume or query patterns that SQLite cannot support safely;
- a richer CLI whose complexity materially exceeds `argparse`;
- platform constraints incompatible with Python 3.12.
