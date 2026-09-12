# Harness Setup

This repository uses a docs-only harness layer. It is a navigation and quality aid, not a runtime system.

## Current Scope

- Root map: [AGENTS.md](../../AGENTS.md)
- Architecture map: [ARCHITECTURE.md](../../ARCHITECTURE.md)
- Quality evidence: [docs/QUALITY.md](../QUALITY.md)
- Known risks: [docs/exec-plans/tech-debt-tracker.md](../exec-plans/tech-debt-tracker.md)

## Pre-Implementation Check

1. Read the domain SSOT first: `README.md`, then the relevant doc under `docs/`.
2. Keep HomeTax live access and write paths disabled unless the user explicitly authorizes them.
3. Preserve write-journal and certificate invariants documented in `docs/ISSUANCE.md`.
4. Run the smallest relevant tests, then broader `uv run pytest` when behavior spans modules.
5. Update docs only when behavior or verified risk changes.

## Out Of Scope

No new dependencies, runtime hooks, CI jobs, `.gitignore`, or Claude settings are created by this docs-only setup.
