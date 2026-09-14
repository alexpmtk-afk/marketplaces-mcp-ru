# Marketplaces MCP — Canonical Architecture

**Status:** CANONICAL  
**Version:** `2026-09-14.v14`

This document mirrors the server-side `core.system_map.SYSTEM_MAP`. The MCP tool `marketplace_system_map` is the machine-readable source of truth exposed to every connected client.

## Runtime path

`ChatGPT / Codex -> marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container -> official WB/Ozon APIs`

Supporting services:
- Secrets: Yandex Lockbox.
- Shared limiter/locks: Yandex Managed Redis/Valkey.
- Canonical marketplace archive: **Google Drive** folder `MCP архив базы данных`.
- Durable queue/job state, staging, immutable annual candidates and resumable-upload state: **Yandex Object Storage**.
- Secondary byte-for-byte backup of canonical archive files: Yandex Object Storage.
- Small Google Drive operations: owner-operated **Google Apps Script** web-app bridge. The bridge executes as the Drive owner and is authenticated by a shared secret kept in Yandex Lockbox.
- Large annual CSV writes: direct **Google Drive API resumable upload** using server-side OAuth material kept in Yandex Lockbox.
- Yandex Object Storage authentication: temporary IAM token obtained by the Serverless Container from its runtime service-account metadata; no static archive key is required.

## Hard boundaries

- Google Cloud is not a runtime provider for Marketplaces MCP. A Google OAuth client may be issued once for Drive authorization, but no MCP workload or archive storage runs in Google Cloud.
- Google Drive annual CSV files and the report registry are the archive source of truth.
- Apps Script remains the preferred narrow bridge for small Drive operations, reads, metadata/status and folder resolution.
- **Large annual CSV files must not be transported through Apps Script as one base64 JSON POST.** They use direct Google Drive API resumable upload.
- Direct Drive OAuth client/refresh-token material lives only in Yandex Lockbox and is injected at runtime. Access tokens, refresh tokens, bridge secrets and resumable session URIs must never be logged.
- Yandex Object Storage must not replace Drive as canonical data; it is used for durable queue/staging, immutable upload candidates, resume state and backup.
- Local files or chat memory are never authoritative shared state.
- No new cloud provider or primary storage path may be introduced without an explicit architecture change.

## Archive rules

- Archive state is server-owned and shared by every client.
- Annual CSV files and the archive registry live canonically on Google Drive under `MCP архив базы данных`.
- Queue state and temporary per-report staging remain in Yandex Object Storage so in-flight jobs survive deployments and client disconnects.
- Large-file finalization is split into durable stages:
  1. `PREPARE` builds one immutable annual candidate in Yandex Object Storage and records its size/SHA256.
  2. `UPLOAD_ANNUAL` creates or resumes a Google Drive resumable session and transfers at most one bounded chunk per worker step.
  3. Google Drive is treated as authoritative for the confirmed byte offset; an interrupted worker queries the resumable session instead of blindly resending the full file.
  4. After the Drive file is complete, its size/checksum are verified.
  5. The same candidate is mirrored byte-for-byte to the canonical Yandex backup path.
  6. Only then does `COMMIT` update `reports_registry.csv` and durable job progress.
- The resumable session URI, confirmed byte offset, target file identity and candidate identity/checksums are durable worker state. Session URIs are bearer-like capabilities and must never be emitted to logs or user responses.
- Non-final resumable chunks must be multiples of 256 KiB. The v1 default is 4 MiB.
- If Drive OAuth for resumable uploads is absent, the job must fail closed in a waiting-for-configuration state while preserving the candidate and existing progress. It must never fall back to the old large Apps Script/base64 upload.
- Existing canonical files left in Yandex by the previous architecture may be migrated to Drive on first content read when Drive does not yet contain that file. Provider data is not re-downloaded for this migration.
- Update is idempotent and registry-driven; already-complete provider reports are not downloaded again.
- WB finance rows are deduplicated by `(reportId, rrdId)` before the annual CSV is written.
- Registry rows are deduplicated by `(cabinet, dataset, report_id)`.
- Partitioning: one logical annual dataset per marketplace / cabinet / dataset / year.
- Annual file pattern: `<cabinet>__<dataset>__<year>.csv`.
- WB weekly finance MAIN uses only `reportType=1` (`Основной`).
- A logical WB week is Monday-Sunday.
- If WB splits one logical week across month/year boundaries, all physical `reportId` fragments belong to that same logical week.
- `reportType=2` (`По выкупам`) is a separate dataset and must never be mixed into MAIN.

## Google Drive access contract

Drive access is intentionally hybrid rather than one-size-fits-all.

### Apps Script bridge — small operations

Runtime configuration:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL` — non-secret `/exec` URL of the deployed web app;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET` — shared secret injected from Yandex Lockbox;
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID` — expected fixed archive root ID.

The bridge supports narrow archive operations such as health/status, folder resolution, named-file stat/read and small writes under the fixed archive root. Server acceptance must verify the exact root ID and root name before the archive is considered reachable.

### Direct Drive API — large annual CSV writes

Runtime configuration:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_OAUTH_JSON` — compact OAuth client/refresh-token JSON injected from Yandex Lockbox;
- `MARKETPLACE_MCP_DRIVE_RESUMABLE_CHUNK_BYTES` — optional chunk size; must be a multiple of 256 KiB; v1 default 4 MiB.

The worker starts a resumable session against the official Google Drive API, stores the opaque session URI only in durable Yandex job state, and transfers at most one chunk per worker step. On interruption it queries Google for the confirmed offset before continuing. This path is required for large annual archive files and must not be replaced by a single base64 Apps Script POST merely to avoid OAuth setup.

## WB Advertising M0

The current advertising phase is **Wildberries only**. Ozon advertising is not part of this phase.

M0 is deliberately read-only and establishes the first safe business vertical:

`named WB cabinet -> dedicated Promotion credential -> live active campaigns -> /adv/v3/fullstats -> normalized advertising-attribution metrics`

Server tools introduced by M0:
- `wb_ads_list_active_campaigns` — live WB campaigns with status `9` (active);
- `wb_ads_get_campaign_stats` — normalized statistics for at most 50 campaign IDs over at most 31 calendar days;
- `wb_ads_audit_active` — audits every currently active campaign over the last 7 full Europe/Moscow calendar days by default.

Advertising credentials are a separate logical credential service named `wb_ads`. Promotion-scoped secrets are stored only server-side through Yandex Lockbox and must not be stored on Google Drive or in GitHub.

M0 metrics have data class `advertising_attribution_operational`. They include provider-attributed spend/orders/order amount and calculated CTR, CPC, click-to-order conversion, CPO, order-based DRR and ROAS. These values **must not be presented as actual business profit**. Actual profitability requires separately approved joins to real orders/sales/buyouts, returns, finance and unit economics.

The current WB fullstats contract accepts at most 50 campaign IDs and a 31-day window. Interactive M0 fails closed rather than returning a partial audit when the active campaign set exceeds one request. Durable rate-aware batching belongs to Advertising Archive V1.

### Advertising archive boundary

The canonical Drive scaffold is `База данных/WB/<cabinet>/<year>/advertising/...`.

The Drive folder scaffold is not evidence that Advertising Archive V1 ingestion, coverage or registry integration exists. Until those mechanisms are implemented and accepted, historical advertising analytics continue to use the approved live provider path. Once coverage is proven, closed historical ad periods become archive-first while current state/control remains live.

### Advertising safety

WB has campaign-control operations implemented as HTTP GETs. HTTP verb does not determine MCP safety. Start, pause and stop are `WRITE`; delete is `DESTRUCTIVE`. Dedicated write/control business tools are not accepted in M0 and will be added only after a separate safety-reviewed phase.

## Semantic Core

The Semantic Core is partially wired into runtime through `marketplace_business_query` and preserves the original natural-language question as the primary intent signal. Legacy `metric` remains compatibility-only and must not override the user's wording.

Canonical components:
- `core/semantic_registry.yaml` — full semantic catalog for the physical WB weekly realization archive;
- `core/semantic_intents.yaml` — deterministic natural-language routing;
- `core/semantic_resolver.py` — fail-closed resolver;
- `core/semantic_execution.yaml` — approved executable contracts;
- `core/semantic_archive.py` — coverage-gated archive execution.

### 92-column weekly-report completion status

The canonical `wb_weekly_finance_main` header has **92 physical columns and all 92 have semantic definitions**. Each field has a documented meaning, role and safe use; the semantic registry fails closed if the physical field set and semantic field catalog diverge. Therefore the original semantic task for the weekly realization report is COMPLETE.

Approved runtime calculations are an additional layer, not a requirement for semantic completeness. Identifiers, flags, historical attributes, percentages and legacy fields must not be turned into totals merely because they exist in the report.

### Approved archive execution

Archive execution requires exact `FULL_COVERAGE` from COMPLETE fragments in `reports_registry.csv` plus presence of the canonical annual file. Current approved archive capabilities are:
- `penalties`;
- `storage_charge`;
- `acceptance_charge`;
- `sale_and_return_operations`;
- `logistics`;
- `deductions_and_adjustments`;
- `commission_and_wb_reward`;
- `acquiring_and_payment_processing`;
- `observed_fulfillment_method`;
- `warehouse_tariff_context`.

Sales/returns use `saleDt` and explicit `docTypeName` buckets, with Продажа minus Возврат for the approved `retailAmount` / `quantity` calculation. Logistics keeps `deliveryService` and `rebillLogisticCost` separate. Deductions keep `deduction` and `additionalPayment` separate and are never silently netted. Monetary WB reward uses `vw` and `vwNds`; it is not derived from `commissionPercent/kvw/kvwBase`. Weekly `acquiringFee` is `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`, not the final monthly acquiring expense.

Historical fulfillment observations use `deliveryMethod`, `officeName` and `rrDate`; their data class is `HISTORICAL_OBSERVED_FULFILLMENT` and they never confirm current fulfillment configuration. Historical warehouse tariff context uses `dlvPrc`, `fixTariffDateFrom`, `fixTariffDateTo`, `warehouseLogisticsCoeff` and `officeName`; its data class is `HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT` and it never confirms the current live warehouse tariff.

## Hard source boundaries

The weekly realization archive is not authoritative for the complete marketplace order funnel, current stock, current fulfillment configuration, current live tariffs, detailed storage drivers, detailed acceptance operations, advertising performance, or Ozon data. Those concepts require another approved source and must fail closed instead of being inferred from weekly rows.

WB Statistics Orders remains operational/preliminary (`PRELIMINARY_NOT_ALL_ORDERS`) and must not be substituted for the complete order flow.

## Routing rules

- “Обнови данные по базе данных” and equivalent intents use the server archive update workflow for all configured cabinets by default.
- Natural business questions preserve the user's original wording and resolve through Semantic Core before source selection.
- Historical questions read the canonical Google Drive archive only when semantic approval and exact coverage exist.
- Current/uncovered periods use a suitable provider API or an explicit backfill/gap workflow; no partial archive is silently substituted.
- Complete-order questions do not substitute WB Statistics Orders for the full marketplace order flow.
- Current tariff questions do not use historical weekly-report coefficients as live tariff truth.
- Advertising current campaign state/control is always live from WB Promotion API.
- Advertising closed-period analytics become archive-first only after Advertising Archive V1 dataset bindings, registry coverage and validation are implemented and accepted.
- All computers/chats see the same remote state; no client may invent its own storage or architecture path.

## Change control

Any architecture change must update all of the following in one change:
1. `core/system_map.py` — `SYSTEM_MAP` and `SYSTEM_INSTRUCTIONS`;
2. this document;
3. `AGENTS.md` when a hard storage/deployment/semantic boundary changes;
4. guardrail tests;
5. CI/security/deployment acceptance.

If an implementation conflicts with this map, fail closed and surface the conflict instead of silently changing architecture.
