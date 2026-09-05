# AGENTS.md

This repository contains **Agentic Engineering Lab**, codename **ARGUS**.

ARGUS is a generic runtime for durable, long-running AI agent missions. Treat the architectural boundary in this file as authoritative unless a reviewed ADR explicitly changes it.

## Core rule

**The orchestrator is deterministic code. LLMs are bounded workers.**

Do not move mission state, retry semantics, budgets, scheduling, or side-effect safety into prompt-only logic.

## Repository scope

ARGUS may own:

- durable mission / step state;
- scheduling and wakeups;
- worker adapters such as OpenCode invocation;
- structured input/output validation;
- retries, timeouts, and typed failures;
- idempotency support and ambiguous-effect recovery primitives;
- correlated events, evidence, and artifact references;
- budgets;
- pause / resume / cancel / kill switch;
- CLI and service control surfaces;
- generic runtime observability.

ARGUS must not own consumer business logic.

For the first reference workload, do **not** add YouTube-, Shorts-, retention-, hook-, editorial-, or publication-policy semantics to this repository. Those remain in Digital Assets Lab.

## Implementation discipline

Before implementing a new abstraction:

1. identify the real consumer operation that requires it;
2. prove the concern is generic;
3. implement the smallest reusable primitive;
4. test restart and failure behavior;
5. document the contract;
6. avoid speculative framework layers.

Prefer a local single-process implementation before distributed architecture unless the current mission explicitly demonstrates the need.

## State and side effects

Persist meaningful state before and after side effects.

Never blindly retry a non-idempotent operation after an ambiguous failure. Model unknown external outcome explicitly and require reconciliation before another attempt.

Completed durable steps must not be re-executed after restart unless their contract explicitly permits it.

## Workers

Treat workers as external, fallible computations.

Worker calls should be bounded by:

- explicit input contract;
- timeout;
- attempt budget;
- output schema;
- error classification;
- cost / duration tracking when available.

Malformed output is a failure, not an invitation to guess what the worker meant.

## Human-over-the-loop

Do not add routine approval gates to autonomous execution unless a consumer policy explicitly requires them.

Escalate only for genuine exceptions such as unresolved policy ambiguity, interactive credentials, exhausted budget, or unreconciled external effects.

## Tests

Changes to runtime semantics should include failure-boundary tests where relevant:

- crash before / after commit;
- restart recovery;
- duplicate wakeup;
- timeout;
- malformed result;
- retryable and permanent failures;
- budget exhaustion;
- pause / kill switch before side effects;
- ambiguous effect reconciliation.

## Security

This repository is public.

Never commit real secrets, tokens, credentials, private consumer data, or sensitive logs. Follow `SECURITY.md`.

## Documentation

Keep `README.md`, `docs/ARCHITECTURE.md`, and `docs/ROADMAP.md` aligned with material architectural changes.

If a change alters a foundational architectural decision, add an ADR rather than silently rewriting history.
