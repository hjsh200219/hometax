# Tech Debt Tracker

This file tracks known risks that already exist in the repository docs and push audit. It does not define new remediation scope.

## Known Risks

| Area | Current Risk | Source |
|---|---|---|
| Issuance compatibility | Actual HomeTax invoice issue/correction/cancel wire compatibility remains unverified for live writes. `raw`/`base64` MagicLine return handling must not be guessed. | [docs/ISSUANCE.md](../ISSUANCE.md) |
| Local cookies | CLI read sessions are short-lived local cookie sessions and are not trusted as signing proof for write operations. Recent changes reauthenticate `--yes` writes with the selected certificate. | [docs/ISSUANCE.md](../ISSUANCE.md) |
| Legacy reconciliation | v1 write-journal rows without content guard hashes cannot prove whether an unresolved old write matches a new issue request, so guarded issue attempts are conservatively blocked until the result is reconciled in HomeTax. | [docs/ISSUANCE.md](../ISSUANCE.md) |
| Counterparty write auth | `counterparty_change --yes` still uses the cached read session (per-certificate since 0.9.0), unlike invoice writes which reauthenticate with the selected certificate. Wrong-business risk is closed by per-certificate session files; signing-key proof is not. Decide whether to align with invoice writes. | 0.9.0 review, [docs/COUNTERPARTIES.md](../COUNTERPARTIES.md) |
| Windows file permissions | Windows has no POSIX mode bits, so `~/.hometax` files are not chmod-restricted and the write journal skips the "parent directory open to group/other" rejection (`write_journal.POSIX_MODES`). Protection relies on the user-profile NTFS ACL; symlink/regular-file checks still apply. Live HomeTax login on Windows is unverified (tests, CLI, and LocalLow certificate discovery verified on Windows 10 x64 Korean, 2026-11-04). | 0.9.2 Windows support |
| Config writes | `~/.hometax/config.toml` (`[certificate]` + `[aliases]`) is read-modify-write without a lock; two terminals changing the default and an alias at the same moment can drop one change. | 0.9.0 review |

## Status Notes

- Do not add automation, dependency, or CI changes from this tracker alone.
