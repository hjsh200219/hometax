# Harness GC History

GC runs record documentation/import evidence independently of live HomeTax compatibility.

| Date | Result | Notes |
|---|---|---|
| 2026-09-12 | initialized | Docs-only harness files created from the push audit and existing repository docs. |
| 2026-09-12 | DONE_WITH_CONCERNS | Quick audit: 31 local links checked, 0 missing; top-level runtime import cycles 0; blockers 0 after fixes. |

## 2026-09-12 — Run 1 (quick, alongside sh-git-push)

- Code commit: `d330dc0` — CLI/issuance/journal fixes and cryptography 50.0.1.
- Tests: 343 passed; Ruff, format, compileall, wheel/sdist passed.
- Documentation: stale API scope/PG claims corrected; AGENTS/CLAUDE map established.
- Architecture: injected/type-only client dependencies clarified; no runtime cycles detected.
- Hygiene: parser method monkeypatch and type-ignore removed; format checked after correction.
- Quality auditor snapshot: 69.3/100 (A 7.75, B 7.33, C 7.0, D 5.0), L3.
  These are evidence-based maintenance ratings, not a production-safety or coverage claim.
- Limits: no configured typechecker, coverage gate or dead-code tool; no live issuance verification.
- No dependencies or runtime infrastructure installed by the harness step.
- First run: no trend comparison or policy-based agent skipping.
