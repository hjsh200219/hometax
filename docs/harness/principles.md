# Harness Principles

These principles keep the repository understandable to agents without turning documentation into a second implementation.

## Principles

- Map, not handbook: root docs point to the source of truth and avoid repeating detailed behavior.
- Evidence first: claims about behavior should cite tests, docs, or code paths.
- Skeptical evaluation: treat completion claims as unproven until fresh evidence supports them.
- Harness simplification: remove harness pieces that no longer help current agents work safely.
- Phase independence: prefer small, self-contained work phases with clear inputs and outputs.
- Sprint contract: agree on intended effect before broad cleanup or risk-bearing changes.
- Scoring anchors: when reviewing quality, tie ratings to concrete examples rather than taste.
- Secret restraint: never capture HomeTax secrets, cookies, tokens, certificate passwords, private keys, or personal tax records in docs.
- Existing SSOT wins: use `docs/PROTOCOL.md`, `docs/ISSUANCE.md`, and peer docs before adding new rules.
- Local reversibility: docs-only harness changes should be easy to inspect and revert.

## Review Dimensions (0–10)

P1 entrypoint; P2 concise map; P3 enforced invariants; P4 conventions;
P5 progressive disclosure; P6 dependency boundaries and test coverage;
P7 repeatable maintenance; P8 error visibility; P9 knowledge in repository;
P10 reproducibility; P11 modularity; P12 explanatory names and contracts.

Score only observed evidence: 0 = absent, 3 = partial/manual, 6 = usable with known gaps,
8 = consistently verified, 10 = measured and enforced. Test counts do not prove coverage.
Missing optional infrastructure is a reported limitation, not permission to install it.
