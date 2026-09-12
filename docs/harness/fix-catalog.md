# Harness Fix Catalog

Use the smallest fix that repairs the documentation gap.

| Problem | Fix |
|---|---|
| Root entrypoint is missing | Add or update `AGENTS.md` as a map with links to existing SSOT docs. |
| Claude-specific entrypoint diverges | Keep `CLAUDE.md` as `@AGENTS.md`. |
| Behavior is duplicated across docs | Keep the detailed contract in the domain doc and link to it from maps. |
| Quality evidence is overstated | Record commands and observed results only; do not invent typecheck or coverage claims. |
| Live HomeTax risk is unclear | Link to `docs/ISSUANCE.md` and the tech-debt tracker, then require explicit live-test authorization. |
| Harness docs grow too large | Move project behavior back to the relevant domain doc or delete stale generic text. |
