# ARGUS bounded workers — Phase 2

Mission #10 workstream #12 introduces the first generic external-worker boundary.

## Principle

ARGUS is the orchestration authority. A model CLI such as OpenCode is a bounded execution backend, not a workflow engine.

The control plane decides:

- which durable step is eligible;
- which worker adapter may execute it;
- timeout and output bounds;
- how the returned outcome is classified later by retry policy;
- which state transition is permitted.

A worker receives one versioned request and must return one versioned response.

## Worker request v1

```json
{
  "schema_version": 1,
  "request_id": "mission/step/attempt",
  "operation": "generic.operation",
  "payload_version": 1,
  "payload": {}
}
```

The payload is opaque to ARGUS and must remain JSON-serializable. Consumer business semantics do not belong in the runtime.

## Worker response v1

```json
{
  "schema_version": 1,
  "request_id": "mission/step/attempt",
  "outcome": "success",
  "output": {},
  "message": null,
  "retry_after_seconds": null,
  "usage": {
    "input_tokens": null,
    "output_tokens": null,
    "cost_usd": null
  }
}
```

Allowed worker-declared outcomes are:

- `success`;
- `retryable_failure`;
- `permanent_failure`.

Transport/runtime outcomes are separate and include process error, timeout, output-limit violation, malformed response, and uncertain process-tree termination.

ARGUS does not infer a valid response from prose or Markdown. Unknown fields, mismatched request IDs, non-finite numbers, malformed JSON, or unsupported schema versions fail visibly.

## Generic JSON subprocess adapter

`JsonSubprocessWorker` launches a configured command, sends the request JSON over stdin, and requires a WorkerResponse JSON object on stdout.

The subprocess transport:

- runs in its own process group/session on POSIX;
- applies a wall-clock timeout;
- attempts TERM then KILL on the whole process group;
- fails closed when descendant termination cannot be proven;
- stores stdout/stderr in temporary files rather than unbounded in-memory pipes;
- retains only configured output prefixes and rejects invocations whose total output exceeds limits;
- does not expose raw payloads in its diagnostic result.

Windows process-tree termination is not claimed by the Phase 2 standard-library implementation. A timeout is therefore reported as `termination_uncertain` on Windows after the root process is stopped. A later platform-specific implementation may strengthen this guarantee.

## OpenCode adapter

`OpenCodeWorkerAdapter` currently targets the documented non-interactive form of OpenCode:

```text
opencode run --model <provider/model> [other flags] <protocol prompt> --file <request.json>
```

The adapter deliberately:

- requires an explicit model instead of silently inheriting a default provider;
- uses a temporary private request attachment so the request payload is not placed in process argv;
- closes stdin for the non-interactive invocation;
- does **not** use OpenCode `--format json`, because that mode is OpenCode's raw event stream rather than the ARGUS WorkerResponse schema;
- requires the model/agent output itself to be exactly one ARGUS WorkerResponse JSON object;
- treats Markdown wrappers or other extra output as malformed instead of heuristically extracting JSON;
- keeps OpenCode-specific flags inside the adapter.

The adapter supports optional `--agent`, `--variant`, `--attach`, `--dir`, and explicit extra arguments. Permission policy is intentionally not auto-enabled by ARGUS at this layer.

OpenCode CLI details can evolve. Deployment should pin or probe the installed version; changes in OpenCode CLI syntax must remain isolated to this adapter.

## Security / privacy

Worker payloads should not contain credentials. Secrets should be supplied through an external secret mechanism or worker environment only when policy permits it.

ARGUS diagnostics keep only bounded stderr excerpts and transport metadata. The default runtime should not persist or print raw worker requests/responses unless a higher-level contract explicitly requires an artifact.

## Still out of scope

Workstream #12 does not yet:

- persist worker attempts;
- decide retry policy;
- reschedule retryable failures;
- reconcile ambiguous external side effects;
- enforce spend budgets;
- pause/cancel workloads.

Those are later workstreams/phases.
