# Marketplaces MCP → Shared Google Drive Bridge v1

Status: integration guide. Production cutover is forbidden until the dedicated Marketplaces deployment passes the common acceptance matrix.

Source of truth for the shared bridge implementation:
`alexpmtk-afk/mcp-yandex-cloud-infra/shared/google-drive-bridge/`

## Required architecture

Marketplaces uses an isolated Bridge v1 deployment:

```text
Marketplaces MCP
→ Marketplaces Redis/Valkey + archive job state
→ project_id=marketplaces
→ dedicated Marketplaces Apps Script deployment
→ dedicated Marketplaces bridge secret
→ dedicated Marketplaces Lockbox binding
→ fixed Drive root: MCP архив базы данных
→ Google Drive
```

Never use Birzha or another project's Apps Script URL, shared secret, Lockbox binding or Drive root.

## Client protocol rules

Every protected request must include:
- `project_id=marketplaces`
- unique `request_id`
- action payload

Every mutation must also include a stable `idempotency_key`. Retries of the same logical mutation reuse the same idempotency key.

Before enabling archive writes, deep health must prove:
- `protocol_version=1`
- `project_id=marketplaces`
- the expected Marketplaces `root_id`
- `fixed_root_file_id_guard=true`

Mismatch means fail closed.

## Responsibilities that remain inside Marketplaces

Do not move into the shared bridge:
- WB/Ozon API logic;
- archive layout and annual CSV naming;
- `reports_registry.csv` semantics;
- PREPARE/UPLOAD/BACKUP/COMMIT workflow;
- Redis/Valkey queues, rate limits and resource/job locks;
- Yandex Object Storage candidates, job state and backups;
- Semantic Core, FULL_COVERAGE and business calculations.

## Large files

- Large uploads use Drive resumable upload. Apps Script is control plane only; large bytes travel Yandex → Google directly.
- Canonical files remain untouched until staging size/SHA256 verification succeeds and the project backup/commit policy is satisfied.
- Large reads must use the accepted Bridge v1 large-download path; whole annual CSV files must not be returned through Apps Script Base64 JSON.

## Security

- all `file_id` operations require fail-closed fixed-root ancestry validation;
- Drive shortcuts outside/through the sandbox are forbidden;
- bridge secret and resumable session URI must never be logged or returned to ChatGPT;
- no global whole-request Apps Script `ScriptLock`;
- resource locks remain in Marketplaces Redis/Valkey.

## Expected code migration points

After the shared bridge reaches an accepted release, adapt only the transport boundary, primarily:
- `core/archive_google.py`
- `core/archive_drive_resumable.py`
- `core/archive_resumable_worker.py`
- `core/archive_resumable_diagnostic.py`
- `core/archive_hybrid.py`
- `core/system_map.py`
- bridge/bootstrap/deploy/storage-validation tests and workflows

Do not redesign Marketplaces business/archive logic as part of this migration.

## Cutover gate

Production switching is allowed only after PASS for:
1. authenticated health with correct project/root/protocol;
2. wrong secret rejection;
3. wrong `project_id` rejection;
4. foreign-root `file_id` rejection;
5. shortcut escape rejection;
6. small exact read/write + SHA256;
7. large resumable upload exact bytes/SHA256;
8. accepted large-download exact bytes/SHA256;
9. safe idempotency replay;
10. concurrent independent resources without shared blocking;
11. retry after ambiguous/lost response without duplicate canonical state.
