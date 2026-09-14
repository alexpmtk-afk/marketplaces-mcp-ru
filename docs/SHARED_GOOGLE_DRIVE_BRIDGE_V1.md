# Marketplaces MCP → Shared Google Drive Bridge v1

Status: FINAL CLIENT CONTRACT. Target: `protocol_version=1`, `bridge_release=1.0.0`. The client implementation is complete. Production activation remains fail-closed until the dedicated Marketplaces deployment passes live acceptance.

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

Before enabling archive writes or reads, deep health must prove:
- `protocol_version=1`
- `project_id=marketplaces`
- the expected Marketplaces `root_id`
- `fixed_root_file_id_guard=true`
- `idempotent_mutations=true`
- `drive_large_download=true`
- `drive_large_download_transport=drive_files_download_lro`

Mismatch means fail closed.

## Responsibilities that remain inside Marketplaces

Do not move into the shared bridge:
- WB/Ozon API logic;
- archive layout and annual CSV naming;
- `reports_registry.csv` semantics;
- `PREPARE/UPLOAD/BACKUP/COMMIT` workflow;
- Redis/Valkey queues, rate limits and resource/job locks;
- Yandex Object Storage candidates, job state and backups;
- Semantic Core, FULL_COVERAGE and business calculations.

## Large uploads

- Large uploads use Drive resumable upload.
- Apps Script is control plane only; large bytes travel Yandex → Google directly.
- Canonical files remain untouched until staging size/SHA256 verification succeeds and the project backup/commit policy is satisfied.

## Large reads — v1.0.0

Canonical annual CSV reads that exceed the bounded Apps Script small-read limit use:

```text
Drive canonical annual CSV
→ large_download_start
→ large_download_poll when required
→ short-lived Google download URI
→ Marketplaces/Yandex client direct GET
→ exact byte-count verification
→ exact SHA256 verification
→ only then expose bytes to archive/business logic
```

Rules:
- Apps Script never carries the whole large file as Base64 JSON.
- `download_uri` is a bearer-like capability and exists only inside the active client call.
- `download_ticket` is opaque and exists only while polling the active call.
- neither URI nor ticket may be logged, persisted to registry/job state, or returned through MCP/ChatGPT.
- the client accepts only HTTPS Google download endpoints.
- file identity, exact size and SHA256 must all pass before downloaded bytes are returned to callers.
- a mismatch is fail-closed: the file must not be used.

## Security

- all `file_id` operations require fail-closed fixed-root ancestry validation;
- Drive shortcuts outside/through the sandbox are forbidden;
- bridge secret, resumable session URI, large-download URI and download ticket must never be logged or returned to ChatGPT;
- no global whole-request Apps Script `ScriptLock`;
- resource locks remain in Marketplaces Redis/Valkey.

## Expected code migration points

Adapt only the transport boundary, primarily:
- `core/archive_google.py`
- `core/archive_drive_resumable.py`
- `core/archive_resumable_worker.py`
- `core/archive_resumable_diagnostic.py`
- `core/archive_hybrid.py`
- `core/system_map.py`
- bridge/bootstrap/deploy/storage-validation tests and workflows

Do not redesign Marketplaces business/archive logic as part of this migration.

`core/system_map.py` must continue to describe the actually deployed production route. Do not change it to Bridge v1 until production cutover is explicitly approved and completed.

## Cutover gate

Production switching is allowed only after PASS for:
1. authenticated health with correct project/root/protocol;
2. wrong secret rejection;
3. wrong `project_id` rejection;
4. foreign-root `file_id` rejection;
5. shortcut escape rejection;
6. small exact read/write + SHA256;
7. large resumable upload exact bytes/SHA256;
8. large direct download exact bytes/SHA256 through `large_download_start/poll`;
9. safe idempotency replay;
10. concurrent independent resources without shared blocking;
11. cross-project concurrent IO with reciprocal root isolation;
12. retry after ambiguous/lost response without duplicate canonical state.

Client code is complete. Production PASS requires only runtime activation evidence: the separate Marketplaces Apps Script deployment, project-isolated `gdrive_bridge_v1_secret`, and live acceptance against real small/large files. No protocol or client function remains unfinished.
