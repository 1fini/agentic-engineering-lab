# Reference Workload: Digital Assets Lab

## Why ARGUS starts with a real workload

Agentic Engineering Lab is intentionally being built from concrete autonomous-system requirements rather than from a speculative general-purpose agent framework.

The first reference workload is the **Autonomous Editorial Learning Loop** in Digital Assets Lab.

The reference workload is useful because it exercises several hard classes of long-running autonomous behavior at once:

- scheduled observation of external data;
- evidence comparison over time;
- LLM-assisted reasoning;
- generation of a new artifact;
- automatic quality checks;
- non-idempotent external side effects;
- delayed verification;
- recovery after interruption;
- repeated cycles without routine human approval.

## Domain loop

At the Digital Assets Lab level, the target loop is conceptually:

```text
observe published artifact
    -> collect real performance evidence
    -> compare against relevant history
    -> diagnose performance
    -> select one bounded improvement hypothesis
    -> generate next artifact
    -> run automatic QA
    -> publish
    -> verify publication
    -> schedule future observation
    -> repeat
```

The important architectural rule is that this loop is **not implemented inside ARGUS as media logic**.

## DAL responsibilities

Digital Assets Lab owns operations such as:

- `FetchYouTubeAnalytics`;
- `AnalyzeShortPerformance`;
- `SelectImprovement`;
- `GenerateNextShort`;
- `EditorialQA`;
- `PublishShort`;
- `UpdateEditorialMemory`.

It also owns all domain-specific schemas and policies, including:

- video and channel identifiers;
- public launch provenance;
- analytics metric definitions;
- comparable-age cohort selection;
- hypothesis semantics;
- editorial constraints;
- factual / rights / narrative QA;
- immutable media hashes;
- publication policy;
- YouTube-specific reconciliation.

## ARGUS responsibilities derived from this workload

The first workload demonstrates a need for these generic capabilities:

### Durable execution journal

Persist before and after each meaningful step so a crash does not force the mission to start again from the beginning.

### Due-at scheduling

External metrics mature over time. A mission must be able to enter `WAITING` with a durable future wakeup.

### Bounded cognitive worker

DAL needs reasoning workers for analysis, hypothesis generation, critique, and possibly generation. ARGUS should invoke those workers with explicit contracts, timeouts, budgets, and structured-result validation.

### Typed failures

A provider timeout, malformed worker result, permanent policy violation, and ambiguous external effect are different states and must not collapse into a generic exception.

### Idempotency and ambiguous-effect handling

Publishing is a useful stress test because a local crash after a successful remote action must not cause a blind duplicate retry.

### Correlated evidence

The consumer needs a complete lineage from observed evidence through hypothesis and generated artifact to the side-effect receipt and later evaluation.

### Budgets and kill switch

A continuously running loop must have deterministic operational limits.

## What must not leak into ARGUS

The following are examples of implementation mistakes:

```text
argus/youtube.py
argus/short_retention.py
argus/editorial_hook_optimizer.py
argus/video_publication_policy.py
```

Those concepts belong in Digital Assets Lab.

ARGUS may instead expose generic primitives such as:

```text
WorkerExecutor
DurableStepStore
DueScheduler
RetryPolicy
BudgetGuard
SideEffectIntent
ReconciliationState
ArtifactReference
```

## First vertical slice

The initial ARGUS implementation should be no larger than required to exercise one real DAL operation across a restart boundary.

A suitable first slice is expected to include:

1. create a mission;
2. persist a runnable step;
3. invoke one bounded worker or deterministic adapter;
4. validate and persist the result;
5. transition to a waiting or next-step state;
6. terminate the process;
7. restart ARGUS;
8. resume without repeating the completed step.

The exact implementation should be driven by the current DAL integration contract, not by this document alone.

## Success criterion

The reference workload succeeds as an ARGUS driver when DAL can consume generic runtime capabilities without requiring ARGUS to know anything about YouTube, Shorts, media performance, or editorial policy.
