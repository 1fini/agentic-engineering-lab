# ADR-0002 — Durable `due_at` scheduling as a SQLite extension

- **Status:** Accepted
- **Date:** 2026-09-08
- **Mission:** #10
- **Workstream:** #11

## Context

ARGUS Phase 1 proved restart-safe execution for ordered steps. The first reference workload now requires a mission to stop making progress until a future time, survive process restart while waiting, and resume only after the persisted due boundary.

The Phase 1 core store intentionally has a small stable mission/step state machine. Adding a `WAITING` core state or changing every persisted step envelope before a concrete need for attempt semantics would widen the migration surface prematurely.

## Decision

Implement Phase 2 scheduling as a versioned extension inside the same SQLite state file:

- `scheduled_steps` stores one canonical UTC `due_at` per mission/step;
- `schedule_events` is an append-only audit history for schedule/reschedule operations;
- `argus_schedule_metadata` versions the extension independently from the Phase 1 core schema;
- timestamps are normalized to UTC ISO-8601 with microseconds and `Z`;
- `DurableScheduler.due(at)` is a read-only eligibility query;
- a scheduled step is eligible only when its `due_at` has passed, its durable step state is `PENDING`, the mission is non-terminal, and every earlier durable step has succeeded;
- repeated due scans do not mutate durable state or create attempts;
- scheduling/rescheduling is permitted only while the step remains `PENDING`.

The scheduler does not execute workers. Attempt creation and claim semantics belong to later Phase 2 workstreams.

## Why no core `WAITING` state yet

For the current single-process reference workload, waiting is a property of future eligibility, not evidence that a worker is active. Keeping the durable step `PENDING` while a separate `due_at` exists preserves the Phase 1 state-machine invariants and avoids treating time passage as execution.

A later mission may add explicit lease/claim/waiting states if a real concurrency or operator-control requirement demonstrates the need.

## Compatibility

The scheduling tables are created lazily in an existing Phase 1 SQLite state file. The Phase 1 store accepts additional tables and remains readable after the extension is initialized. No Phase 1 table or persisted envelope is rewritten by this workstream.

The extension has its own schema version. Unsupported or malformed extension state fails closed.

## Consequences

- Phase 1 state files can gain scheduling capability without destructive migration.
- An ARGUS caller must consult scheduler eligibility before executing scheduled work; integration with the execution/attempt loop is completed later in Mission #10.
- Duplicate scheduler scans are safe because they are read-only.
- This design does **not** claim distributed uniqueness or exactly-once scheduling.
- External side-effect safety remains explicitly out of scope.
