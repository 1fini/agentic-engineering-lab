# Contributing to Agentic Engineering Lab

Agentic Engineering Lab is in an early architecture and runtime-foundation phase.

Contributions should optimize for clarity, durability, and evidence over framework breadth.

## Before proposing a change

Ask:

1. Which real workload requires this capability?
2. Is the concern generic to agent execution, or does it belong in the consumer application?
3. What is the smallest durable primitive that solves the demonstrated need?
4. How will the capability behave across process failure and restart?
5. How will an operator inspect what happened afterward?

If a proposal cannot answer those questions, it is probably too speculative for ARGUS today.

## Architectural boundary

ARGUS should contain reusable runtime concerns such as:

- durable state;
- scheduling;
- worker execution;
- structured-result validation;
- retry / timeout semantics;
- side-effect recovery protocols;
- observability;
- budgets and operational controls.

Consumer-specific business logic stays in the consumer.

For example, Digital Assets Lab owns YouTube, Shorts, editorial analytics, publication semantics, and editorial policy. ARGUS must not acquire those concepts merely because DAL is the first reference workload.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Change size

Prefer small, reviewable vertical slices over broad framework PRs.

A good change should normally have:

- one clear capability;
- an explicit failure model;
- focused tests;
- documentation for any public contract;
- no unrelated refactoring.

## Tests

Runtime behavior must be tested at failure boundaries, not only on the happy path.

Depending on the feature, useful cases include:

- process termination before a state commit;
- process termination after a state commit;
- duplicate wakeup;
- malformed worker output;
- retryable provider failure;
- permanent failure;
- timeout and process-tree cleanup;
- budget exhaustion;
- pause before a side effect;
- ambiguous external outcome;
- restart and resume without repeating completed work.

## Documentation

Public architectural decisions should be captured in documentation or ADRs rather than living only in issue comments.

Do not silently change foundational boundaries such as:

- deterministic orchestrator vs LLM worker;
- ARGUS vs consumer ownership;
- human-over-the-loop policy model;
- durable side-effect handling.

## Security

Read [SECURITY.md](SECURITY.md) before adding integrations, credentials, logging, worker execution, or example configuration.

Never commit secrets or private consumer data.

## Licensing

No open-source license has been selected yet. Contributions should not introduce copied code or assets with incompatible or unclear licensing.
