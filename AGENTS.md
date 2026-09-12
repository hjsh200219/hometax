# AGENTS.md

This repository is `hometax`, a Python 3.12 CLI/library/local API for HomeTax certificate login, read-only tax/finance queries, counterparty previews, and guarded tax-invoice write flows.

## Map

- Product and usage entrypoint: [README.md](./README.md)
- Runtime dependency map: [ARCHITECTURE.md](./ARCHITECTURE.md)
- Quality evidence and checks: [docs/QUALITY.md](./docs/QUALITY.md)
- Known risks and execution notes: [docs/exec-plans/tech-debt-tracker.md](./docs/exec-plans/tech-debt-tracker.md)
- Protocol and HomeTax evidence: [docs/PROTOCOL.md](./docs/PROTOCOL.md)
- Invoice issuance invariants: [docs/ISSUANCE.md](./docs/ISSUANCE.md)
- Invoice reads: [docs/INVOICES.md](./docs/INVOICES.md)
- Counterparty flows: [docs/COUNTERPARTIES.md](./docs/COUNTERPARTIES.md)
- Financial read flows: [docs/FINANCIALS.md](./docs/FINANCIALS.md)
- Harness docs: [docs/harness/harness-setup.md](./docs/harness/harness-setup.md)

- Do not record certificate passwords, private keys, cookies, tokens, raw signed XML, or personal tax data in docs, tests, logs, or commits.
- Treat HomeTax write flows as preview-first and journal-guarded; keep `--yes`, wire-profile, certificate, digest, and journal checks aligned with `docs/ISSUANCE.md`.
- Keep tests network-free unless a user explicitly authorizes live HomeTax verification.
- Do not add runtime dependencies, CI, or deployment behavior from harness docs alone.

## 세션 시작 시 필수
Read [.claude-project/HANDOFF.md](./.claude-project/HANDOFF.md) before work.

## Health Stack

- Install/sync: `uv sync --locked`
- Tests: `uv run pytest`
- Lint/format check: `uv run ruff check .` and `uv run ruff format --check .`
