# Marketplaces MCP — Canonical Architecture

**Status:** CANONICAL  
**Version:** `2026-09-14.v9`

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

The Semantic Core is now partially connected to the runtime business-query entry point.

Current components:
- `core/semantic_registry.yaml` — semantic passport of the currently available archive dataset and all 92 physical fields of the WB weekly realization detail;
- `core/semantic_intents.yaml` — deterministic mapping of common business wording to registered capabilities or to a required external source;
- `core/semantic_resolver.py` — fail-closed resolver producing one of: `AVAILABLE`, `AVAILABLE_WITH_LIMITATION`, `REQUIRES_OTHER_SOURCE`, `AMBIGUOUS`, `UNKNOWN`;
- `core/semantic_execution.yaml` — explicit allow-list of business capabilities that are approved for archive calculation;
- `core/semantic_archive.py` — coverage evaluator, safe SQL planner and archive executor for approved calculations;
- `marketplace_business_query` — runtime entry point that now accepts the user's original question and routes approved semantic capabilities.

Current database truth:
- `wb_weekly_finance_main` is the only business report treated as present in the canonical archive;
- other WB/Ozon reports are reference-only until they are actually archived and registered as available.

Current approved archive calculations:
- `penalties` — sum `penalty` exactly as reported, grouped by report currency and reason;
- `storage_charge` — sum `paidStorage` exactly as reported by report currency;
- `acceptance_charge` — sum `paidAcceptance` exactly as reported by report currency;
- `sale_and_return_operations` — by `saleDt`, split rows by `docTypeName`: `Продажа` and `Возврат`; calculate sale amount/units, return amount/units and net result as `Продажа - Возврат` using `retailAmount` and `quantity`.

The sales/returns formula follows the official Wildberries weekly-realization rule: the weekly `Продажа` amount is the detailed report's realized-goods amount for document type `Продажа` minus the same amount for document type `Возврат`. The canonical archive also confirms that return `retailAmount` values are stored as positive values, so the server performs the subtraction explicitly rather than inferring a sign.

Every approved calculation is subject to these gates:
1. the original question must resolve to the registered capability;
2. the capability must be explicitly present in `semantic_execution.yaml`;
3. `reports_registry.csv` must prove `FULL_COVERAGE` for the entire requested period using `COMPLETE` fragments;
4. the referenced canonical annual file must exist on Google Drive;
5. only registered fields and a generated read-only query plan may be used;
6. different currencies are never combined into one amount;
7. formulas that require operation-type subtraction must encode that subtraction explicitly rather than infer it from the sign of a monetary field.

### Natural-question routing

`marketplace_business_query` prefers the user's original natural-language question. The older `metric` parameter remains only for backward compatibility.

Rules:
- the original question outranks a conflicting legacy metric hint;
- if the question maps to one of the approved archive calculations, the request goes through the coverage-gated archive executor;
- sales/returns questions use `saleDt` and explicit `docTypeName` buckets, not `orderDt` and not free-form operation-name guessing;
- if the question is understood but its calculation contract is not approved, execution stops rather than guessing;
- if the required source is not in the current database, the server returns that source requirement instead of substituting a similar field/report;
- ambiguous or unknown questions fail closed.

### Orders guardrail

Wildberries officially describes `/api/v1/supplier/orders` as an operational/preliminary source. It may omit orders for which payment is not confirmed, including delayed or installment-payment cases. Therefore:
- this feed may still be used by the legacy operational route;
- any result from it is explicitly marked `PRELIMINARY_NOT_ALL_ORDERS`;
- a natural question meaning “all/complete orders” must **not** be answered from that feed;
- such a question remains `REQUIRES_OTHER_SOURCE` until the complete Order Feed (or another officially equivalent source) is available and approved.

This prevents the server from confusing “data available from an operational API” with “complete business truth”.

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
- Business questions preserve the original wording and are semantically resolved before archive fields or another source are selected.
- Historical archive calculation is allowed only for an explicitly approved capability with `FULL_COVERAGE` and canonical annual-file presence.
- Current/uncovered periods use an explicitly suitable provider/API source or return a source/coverage gap; partial archive data is never silently returned as complete.
- Complete-order questions never fall back to WB Statistics Orders.
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
