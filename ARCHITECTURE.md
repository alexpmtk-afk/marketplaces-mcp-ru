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

## WB Advertising M0

The current advertising phase is **Wildberries only**. Ozon advertising is not part of this phase.

M0 is deliberately read-only and establishes the first safe business vertical:

`named WB cabinet -> dedicated Promotion credential -> live active campaigns -> /adv/v3/fullstats -> normalized advertising-attribution metrics`

Server tools introduced by M0:
- `wb_ads_list_active_campaigns` — live WB campaigns with status `9` (active);
- `wb_ads_get_campaign_stats` — normalized statistics for at most 50 campaign IDs over at most 31 calendar days;
- `wb_ads_audit_active` — audits every currently active campaign over the last 7 full Europe/Moscow calendar days by default.

Advertising credentials are a separate logical credential service named `wb_ads`. They use the same canonical business cabinet names (`wb_dmitrieva`, `wb_novokshenov`, `wb_laser_master`) but Promotion-scoped secrets are stored only server-side through the deployment-managed secret architecture/Yandex Lockbox. They must not be stored on Google Drive or in GitHub.

M0 metrics have data class `advertising_attribution_operational`. They include provider-attributed spend/orders/order amount and calculated CTR, CPC, click-to-order conversion, CPO, order-based DRR and ROAS. These values **must not be presented as actual business profit**. Actual profitability requires separately approved joins to real orders/sales/buyouts, returns, finance and unit economics.

The current WB fullstats contract accepts at most 50 campaign IDs and a 31-day window. Interactive M0 fails closed rather than returning a partial audit when the active campaign set exceeds one request. Durable rate-aware batching belongs to Advertising Archive V1.

### Advertising archive boundary

The canonical Drive scaffold is:

`База данных/WB/<cabinet>/<year>/advertising/...`

The intended dataset families are campaign/product/search-cluster daily statistics, campaign snapshots, financial expenses/payments, bid/product/placement/minus-phrase history and MCP action audit records.

**Important:** the Drive folder scaffold is not evidence that Advertising Archive V1 ingestion, coverage or registry integration exists. Until those mechanisms are implemented and accepted, historical advertising analytics continue to use the approved live provider path. Once coverage is proven, closed historical ad periods become archive-first while current state/control remains live.

### Advertising safety

WB has campaign-control operations implemented as HTTP GETs. HTTP verb does not determine MCP safety. Start, pause and stop are `WRITE`; delete is `DESTRUCTIVE`. Dedicated write/control business tools are not accepted in M0 and will be added only after a separate safety-reviewed phase.

## Google Drive bridge contract

The Yandex-hosted MCP talks to one deployed Apps Script web app. Runtime configuration is:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL` — non-secret `/exec` URL of the deployed web app;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET` — shared secret injected from Yandex Lockbox;
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID` — expected fixed archive root ID.

The bridge supports only narrow archive operations: health/status, named-file stat/read/write under the fixed archive root, and explicit file reads required by the storage interface. Server acceptance must verify the exact root ID and root name before the archive is considered reachable.

## Routing rules

- “Обнови данные по базе данных” and equivalent intents use the server archive update workflow for all configured cabinets by default.
- Historical questions read the canonical Google Drive archive when coverage exists.
- Current/uncovered periods use provider APIs or an explicit backfill/gap workflow.
- Advertising current campaign state/control is always live from WB Promotion API.
- Advertising closed-period analytics become archive-first only after Advertising Archive V1 dataset bindings, registry coverage and validation are implemented and accepted.
- All computers/chats see the same remote state; no client may invent its own storage or architecture path.

## Change control

Any architecture change must update all of the following in one change:
1. `core/system_map.py` — `SYSTEM_MAP` and `SYSTEM_INSTRUCTIONS`;
2. this document;
3. guardrail tests;
4. CI/security/deployment acceptance.

If an implementation conflicts with this map, fail closed and surface the conflict instead of silently changing architecture.
