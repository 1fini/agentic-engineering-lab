# ARGUS Development

## Phase 1 local setup

ARGUS currently targets Python 3.12 or newer.

Create and activate a virtual environment, then install the package in editable mode with the development extra:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

On Windows PowerShell, activate the environment with `.venv\\Scripts\\Activate.ps1`.

## Tests

Run the complete current test suite with:

```bash
python -m pytest
```

The Phase 1 CI runs the same package installation and tests on Python 3.12, followed by a CLI smoke test:

```bash
argus --version
```

## Repository layout

```text
src/argus/       runtime package
tests/           executable behavior tests
docs/adr/        architectural decisions
.github/workflows/ci.yml
```

The `src/` layout is intentional. Import ARGUS through an installed package rather than relying on the repository root being added to `PYTHONPATH` implicitly.

## Persistence direction

ADR-0001 selects the standard-library `sqlite3` module and a local SQLite database for the Phase 1 durable mission store. The schema and transaction model are introduced in workstream #3, not in the foundation workstream.

## Scope discipline

Do not add scheduler, OpenCode integration, workflow DSLs, distributed execution, or consumer business semantics while working on the Phase 1 foundation unless the active mission contract explicitly advances to those workstreams.
