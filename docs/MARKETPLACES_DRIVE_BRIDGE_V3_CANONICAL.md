# Marketplaces Google Drive Bridge v3 — canonical

Status: CANONICAL for Marketplaces MCP.

Marketplaces uses only the project-specific Google Apps Script Drive Bridge v3 between Yandex Cloud and the canonical Google Drive archive.

Required runtime variables:

- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=v3`
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL`
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET`
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID`

Shared Google Drive Bridge Protocol v1 is retired for Marketplaces and must not be selected, deployed, or bound into the Marketplaces runtime.

## Archive invariants

- Google Drive remains the canonical business archive.
- The bridge is restricted to the fixed Marketplaces archive root.
- Large annual uploads use Google Drive resumable sessions and immutable Yandex candidates.
- The canonical Drive file is not replaced until exact byte count and SHA256 are verified.
- `promote_verified` is safe to replay after an ambiguous/lost response.
- Bounded small writes are replay-safe for identical content.
- Read-only bridge actions do not require the mutation lock.
- The client retries transient Apps Script redirect/transport failures, including temporary 404/408/425/429/5xx responses.

## Scope boundary

This decision applies only to Marketplaces MCP. Shared Bridge v1 assets used by other projects such as Birzha are outside this decision and must not be removed by Marketplaces cleanup.
