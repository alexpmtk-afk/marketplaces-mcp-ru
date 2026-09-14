# Marketplaces MCP — Canonical Architecture

**Status:** CANONICAL  
**Version:** `2026-09-14.v5`

This document mirrors the server-side `core.system_map.SYSTEM_MAP`. The MCP tool `marketplace_system_map` is the machine-readable source of truth exposed to every connected client.

## Runtime path

`ChatGPT / Codex -> marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container -> official WB/Ozon APIs`

Supporting services:
- Secrets: Yandex Lockbox.
- Shared limiter/locks: Yandex Managed Redis/Valkey.
- Canonical marketplace archive: **Google Drive** folder `MCP архив базы данных`.
- Durable queue/job state and staging: **Yandex Object Storage**.
- Secondary byte-for-byte backup of canonical archive files: Yandex Object Storage.
- Google Drive access: owner-operated **Google Apps Script web-app bridge**. The bridge executes as the Drive owner and is authenticated by a shared secret kept in Yandex Lockbox.
- Yandex Object Storage authentication: temporary IAM token obtained by the Serverless Container from its runtime service-account metadata; no static archive key is required.

## Hard boundaries

- Google Cloud is not part of the runtime architecture. The MCP does not depend on a Google Cloud OAuth refresh token.
- Google Drive annual CSV files and the report registry are the archive source of truth.
- The Apps Script bridge is only a transport/authentication surface into the fixed Drive archive root; it is not a second source of truth.
- Yandex Object Storage must not replace Drive as canonical data; it is used for durable queue/staging and backup.
- Local files or chat memory are never authoritative shared state.
- No new cloud provider or primary storage path may be introduced without an explicit architecture change.

## Archive rules

- Archive state is server-owned and shared by every client.
- Annual CSV files and the archive registry live canonically on Google Drive under `MCP архив базы данных`.
- Each canonical write is committed to Google Drive first through the Apps Script bridge and then copied byte-for-byte to Yandex Object Storage as a backup.
- Existing canonical files left in Yandex by the previous architecture are migrated to Google Drive on first read when Drive does not yet contain that file. Provider data is not re-downloaded for this migration.
- Queue state and temporary per-report staging remain in Yandex Object Storage so in-flight jobs survive deployments and client disconnects.
- Update is idempotent and registry-driven; already-complete provider reports are not downloaded again.
- WB finance rows are deduplicated by `(reportId, rrdId)` before the annual CSV is written.
- Registry rows are deduplicated by `(cabinet, dataset, report_id)`.
- Partitioning: one logical annual dataset per marketplace / cabinet / dataset / year.
- Annual file pattern: `<cabinet>__<dataset>__<year>.csv`.
- WB weekly finance MAIN uses only `reportType=1` (`Основной`).
- A logical WB week is Monday-Sunday.
- If WB splits one logical week across month/year boundaries, all physical `reportId` fragments belong to that same logical week.
- `reportType=2` (`По выкупам`) is a separate dataset and must never be mixed into MAIN.

## Semantic Core

The Semantic Core is a server-side knowledge and routing layer that sits before business query execution.

Current components:
- `core/semantic_registry.yaml` — semantic passport of the currently available archive dataset and all 92 physical fields of the WB weekly realization detail;
- `core/semantic_intents.yaml` — deterministic mapping of common business wording to registered capabilities or to a required external source;
- `core/semantic_resolver.py` — fail-closed resolver producing one of: `AVAILABLE`, `AVAILABLE_WITH_LIMITATION`, `REQUIRES_OTHER_SOURCE`, `AMBIGUOUS`, `UNKNOWN`.

Current database truth:
- `wb_weekly_finance_main` is the only business report treated as present in the canonical archive;
- other WB/Ozon reports are reference-only until they are actually archived and registered as available.

Guardrails:
- exact physical field questions may resolve directly to that field's registered semantics;
- complete marketplace orders must never be reconstructed from `orderDt`/`orderUid` in weekly finance;
- `deliveryMethod` and warehouse/tariff fields describe historical reported operations and must not be presented as current seller/product configuration;
- explicit current-state questions do not fall back to historical weekly finance;
- unknown or ambiguous requests fail closed;
- semantic resolution does not itself execute archive SQL; a later query plan must still verify `FULL_COVERAGE` before archive execution.

The resolver is **not yet wired into `marketplace_business_query` runtime execution**. That integration is a separate controlled step.

## Google Drive bridge contract

The Yandex-hosted MCP talks to one deployed Apps Script web app. Runtime configuration is:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL` — non-secret `/exec` URL of the deployed web app;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET` — shared secret injected from Yandex Lockbox;
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID` — expected fixed archive root ID.

The bridge supports only narrow archive operations: health/status, named-file stat/read/write under the fixed archive root, and explicit file reads required by the storage interface. Server acceptance must verify the exact root ID and root name before the archive is considered reachable.

## Routing rules

- “Обнови данные по базе данных” and equivalent intents use the server archive update workflow for all configured cabinets by default.
- Business questions are semantically resolved before archive fields or another source are selected.
- Historical questions read the canonical Google Drive archive when the semantic capability is registered and coverage exists.
- Current/uncovered periods use provider APIs or an explicit gap/backfill workflow.
- All computers/chats see the same remote state; no client may invent its own storage or architecture path.

## Change control

Any architecture change must update all of the following in one change:
1. `core/system_map.py` — `SYSTEM_MAP` and `SYSTEM_INSTRUCTIONS`;
2. this document;
3. guardrail tests;
4. CI/security/deployment acceptance.

If an implementation conflicts with this map, fail closed and surface the conflict instead of silently changing architecture.
