# Architecture

`hometax` is a Python package with three public surfaces over the same implementation: the `hometax` CLI, the optional FastAPI local API, and direct library imports.

## Entry Points

- `src/hometax_login/cli.py` parses CLI commands, selects certificates, restores or creates sessions, formats output, and calls domain services.
- `src/hometax_login/api.py` exposes the local HTTP API for certificate validation, sessions, invoice queries, financial queries, counterparties, and invoice-operation previews/submits.
- `src/hometax_login/protocol.py` owns the HomeTax HTTP client, login flow, cookie jar, ET/mobile token handoffs, and upstream request helpers.

## Domain Modules

- Certificate handling: `cert_discovery.py` finds NPKI/PFX candidates; `certificates.py` decrypts and validates certificate material.
- Sessions: `sessions.py` models API sessions; `local_session.py` stores the short-lived CLI cookie session on disk.
- Invoice reads: `invoices.py` maps tax-invoice list, summary, and detail reads. See [docs/INVOICES.md](./docs/INVOICES.md).
- Financial reads: `financials.py` maps business-card, card-sales, cash-receipt, and business-account read actions. See [docs/FINANCIALS.md](./docs/FINANCIALS.md).
- Counterparty changes: `counterparty_changes.py` defines request models and preview contracts; `counterparty_backend.py` maps HomeTax counterparty actions.
- Invoice issuance: `issuance_models.py` defines draft/correction/cancel models; `issuance_backend.py` prepares HomeTax XML/actions; `invoice_signing.py` signs XML; `invoice_operations.py` coordinates preview/submit; `write_journal.py` protects uncertain writes.
- Errors: `errors.py` carries structured command/API failures.

## Dependency Direction

`cli.py` and `api.py` are adapters depending on protocol, domain models, and services.
Domain services receive clients/backends by injection; references to the protocol client for
typing use `TYPE_CHECKING`, avoiding a top-level runtime cycle. `protocol.py` constructs its
domain helpers. Certificate loading, signing, and journal code do not depend on CLI/FastAPI adapters.

## Persistence And Secrets

User-local state lives under `~/.hometax/`: selected certificate path/fingerprint, short-lived session cookies, and the SQLite write journal. Certificate passwords and private keys are not stored. Tests use synthetic fixtures and must not require live HomeTax network access by default.

## External Dependencies

Runtime dependencies are declared in `pyproject.toml`: `asn1crypto`, `cryptography`, `fastapi`, `httpx`, `lxml`, `uvicorn`, and `xmlsec`. Dev dependencies are `pytest`, `pytest-asyncio`, and `ruff`.
