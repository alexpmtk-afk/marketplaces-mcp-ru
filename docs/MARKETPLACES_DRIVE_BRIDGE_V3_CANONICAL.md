# Marketplaces Google Drive Bridge v3 — canonical

Status: CANONICAL for Marketplaces MCP.

Marketplaces uses the project-specific Google Apps Script Drive Bridge v3 between the current REMOTE Marketplaces runtime and the canonical Google Drive archive. Yandex Cloud is no longer the production runtime path.

Required runtime variables:

- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=v3`
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL`
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET`
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID`

Shared Google Drive Bridge Protocol v1 is retired for Marketplaces and must not be selected, deployed, or bound into the Marketplaces runtime.

## Archive invariants

- Google Drive remains the canonical business archive.
- The bridge is restricted to the fixed Marketplaces archive root.
- Large annual uploads use Google Drive resumable sessions and immutable candidates from the currently configured durable backend.
- The canonical Drive file is not replaced until exact byte count and SHA256 are verified.
- `promote_verified` is safe to replay after an ambiguous/lost response.
- Bounded small writes are replay-safe for identical content.
- Read-only bridge actions do not require the mutation lock.
- The client retries transient Apps Script redirect/transport failures, including temporary 404/408/425/429/5xx responses.

## REMOTE runtime boundary

The Bridge URL, shared secret and archive root are injected server-side on REMOTE. The shared secret must not be sourced from Yandex Lockbox merely because legacy deployment code still supports that path.

Before any mutating archive operation after the migration, establish the actual durable backend from the REMOTE service environment/config. Legacy Yandex Object Storage code may remain for migration compatibility but is not evidence of an active production backend.

Read-only historical queries are different: once Bridge v3 URL/secret/root are configured,
canonical Google Drive data must remain readable even when the former Yandex Object Storage
backend is absent.  In that state queue/staging/candidate/backup mutations fail closed with
`DURABLE_BACKEND_NOT_CONFIGURED`; the service must not report the canonical archive itself
as absent.

## Scope boundary

This decision applies only to Marketplaces MCP. Shared Bridge v1 assets used by other projects such as Birzha are outside this decision and must not be removed by Marketplaces cleanup.