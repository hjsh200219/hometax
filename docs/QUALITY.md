# Quality

The current local validation record from the push audit states that the full test suite passed with 343 tests, along with Ruff, format, compileall, and package build checks. This document records that evidence without expanding it into a new CI or coverage contract.

## Configured Checks

- Tests: `uv run pytest`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Packaging/build checks are manual project validation steps, not configured CI in this repository.

## Current Limits

- No configured typechecker is present in `pyproject.toml`.
- No coverage threshold is configured, and no coverage percentage is claimed here.
- Live HomeTax login or write compatibility is not part of the default test harness.

## Test Boundaries

Default tests should remain deterministic and network-free. Live HomeTax checks need explicit authorization because they can touch personal tax sessions or real write paths.
