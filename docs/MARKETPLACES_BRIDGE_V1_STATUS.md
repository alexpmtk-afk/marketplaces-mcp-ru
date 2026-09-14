# Marketplaces Google Drive Bridge v1 status

Release target: `protocol_version=1`, `bridge_release=1.0.0`.

## Code state

**CODE COMPLETE.** The Marketplaces archive client implements:
- project-pinned `project_id=marketplaces`;
- unique request IDs and stable idempotency keys;
- fixed-root health validation;
- bounded small I/O;
- resumable large upload;
- verified promotion with exact size/SHA256;
- direct large Google download through Bridge `files.download` LRO;
- exact byte/SHA256 verification before archive bytes are exposed;
- explicit legacy rollback profile with no automatic credential fallback.

## Runtime order

1. Deploy/authorize the dedicated Marketplaces Apps Script Bridge v1.0.0.
2. Store the generated project secret in the Marketplaces project Lockbox under `gdrive_bridge_v1_secret`.
3. Run live health/security/small/large/concurrency acceptance.
4. Set `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_PROTOCOL=v1` and inject the v1 URL/secret/root.
5. Run post-cutover archive read/write verification.
6. Retain the legacy variables only for an explicit rollback window, then remove them after stable post-cutover evidence.

Interactive Google owner authorization is an external runtime permission gate, not unfinished code.
