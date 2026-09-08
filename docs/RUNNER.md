# ARGUS Phase 1 Runner and CLI

Mission #1 workstream #4 adds the first deterministic execution surface on top of the durable state contract.

## Runner semantics

`MissionRunner` receives two explicit dependencies:

- a durable `SqliteMissionStore`;
- a `WorkerRegistry` mapping operation names to bounded Python callables.

The runner never asks an LLM what to do next. It reads persisted state and selects the next eligible step by durable ordinal.

Normal execution is:

```text
mission PENDING -> RUNNING
step PENDING -> RUNNING
worker returns JSON-compatible mapping
step RUNNING -> SUCCEEDED + durable result
...
mission RUNNING -> COMPLETED
```

Already-succeeded steps are skipped. Re-running a completed mission is a no-op.

An explicit `StepExecutionFailure` is persisted as a failed step/result and fails the mission. A worker output that cannot be serialized as a Phase 1 result is also a deterministic contract failure.

An unexpected worker exception is intentionally different: the step has already entered `RUNNING`, so ARGUS leaves it there and raises `WorkerInterruptedError`. Workstream #5 owns restart/recovery semantics for that ambiguous execution boundary; #4 does not guess or rerun it.

An unregistered operation blocks before the step enters `RUNNING`.

## Mission manifest v1

The CLI can create/resume a fixture mission from a JSON manifest:

```json
{
  "schema_version": 1,
  "mission_id": "example",
  "steps": [
    {
      "step_id": "one",
      "operation": "fixture.echo",
      "payload_version": 1,
      "payload": {"value": 1}
    }
  ]
}
```

Step ordinal is derived from array order. The manifest is a generic execution fixture, not a workflow DSL.

## CLI

Phase 1 commands emit compact JSON suitable for automation:

```bash
argus run --store .argus/state.db --manifest mission.json --fixture-workers
argus status --store .argus/state.db example
argus inspect --store .argus/state.db example
```

`--fixture-workers` enables only the explicit generic test/demo registry (`fixture.echo`, `fixture.fail`). It is not a production worker backend. OpenCode integration belongs to a later ARGUS mission.

`inspect` intentionally does not print raw consumer payloads by default; it exposes step identity, operation, state, idempotency key, result kind, and the journal.

## Exit codes

- `0` — command succeeded / mission completed;
- `2` — invalid manifest/input;
- `3` — durable state error or unknown mission;
- `4` — mission blocked because safe progress is not currently possible;
- `5` — mission reached a durable failed state.

Argparse may also use exit code `2` for malformed command-line syntax.

## Still out of scope

- recovering a `RUNNING` step after process death;
- due-at scheduling;
- model/OpenCode workers;
- retry budgets and timeout classification;
- external side-effect reconciliation;
- DAL/YouTube/editorial semantics.
