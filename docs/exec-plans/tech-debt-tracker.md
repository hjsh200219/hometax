# Tech Debt Tracker

This file tracks known risks that already exist in the repository docs and push audit. It does not define new remediation scope.

## Known Risks

| Area | Current Risk | Source |
|---|---|---|
| Issuance compatibility | Actual HomeTax invoice issue/correction/cancel wire compatibility remains unverified for live writes. `raw`/`base64` MagicLine return handling must not be guessed. | [docs/ISSUANCE.md](../ISSUANCE.md) |
| Local cookies | CLI read sessions are short-lived local cookie sessions and are not trusted as signing proof for write operations. Recent changes reauthenticate `--yes` writes with the selected certificate. | [docs/ISSUANCE.md](../ISSUANCE.md) |
| Legacy reconciliation | v1 write-journal rows without content guard hashes cannot prove whether an unresolved old write matches a new issue request, so guarded issue attempts are conservatively blocked until the result is reconciled in HomeTax. | [docs/ISSUANCE.md](../ISSUANCE.md) |

## Status Notes

- Do not add automation, dependency, or CI changes from this tracker alone.
