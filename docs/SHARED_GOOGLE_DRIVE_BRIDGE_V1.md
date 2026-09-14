# Marketplaces → Google Drive Bridge Protocol v1

Status: **canonical application runtime**.

The Marketplaces application has one Google Drive protocol path:

`Marketplaces MCP → project queue/locks → Protocol v1 client → isolated Marketplaces Apps Script 1.0.0 → fixed root MCP архив базы данных`.

Required runtime variables:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_URL`;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_SECRET`;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_V1_ROOT_ID`.

`project_id=marketplaces` is hard-pinned in code. Every mutation uses a stable idempotency key and every request has a unique request id. Large uploads use Drive resumable upload and large downloads use the Bridge-brokered Drive files.download LRO with exact size/SHA256 verification.

There is no application-level legacy/v3 fallback. Operational rollback means switching the Yandex Serverless Container to the previous known-good revision/image; it does not mean selecting a second protocol inside the current code.

Central protocol source of truth: `alexpmtk-afk/mcp-yandex-cloud-infra/shared/google-drive-bridge/`, release `1.0.0`.
