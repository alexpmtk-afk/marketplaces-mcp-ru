# Marketplaces MCP — Canonical Architecture

**Status:** CANONICAL
**Version:** `2026-09-23.v22`

This document mirrors the server-side `core.system_map.SYSTEM_MAP`. The MCP tool `marketplace_system_map` is the machine-readable source of truth exposed to every connected client.

## Runtime path

Current production runs on the dedicated Linux REMOTE server:

- host: `VM-684381` / `89.208.14.36`;
- root: `/opt/mcp/`;
- Marketplaces service: `mcp-marketplaces.service`;
- service user: `mcp-marketplaces`;
- internal MCP: `http://127.0.0.1:8080/mcp`;
- local Redis: `127.0.0.1:6379`.

Approved external path:

`Codex / allowed client -> https://mcp892081436.duckdns.org:13267/mcp -> OAuth/Keycloak -> REMOTE GPT-MCP bridge (127.0.0.1:18181) -> internal Marketplaces MCP -> official WB/Ozon APIs`

The legacy `marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container` route is **retired as a production path** and must not be used as fallback.

Production secrets are server-side on REMOTE under `/opt/mcp/secrets/marketplaces/` with restricted permissions. Yandex Lockbox must not be described as the current production secret store.

Supporting services:
- Canonical marketplace archive: **Google Drive** folder `Мой диск/Marketplaces/MCP отчеты МП/MCP архив базы данных`.
- Shared limiter/locks/queue coordination: local REMOTE Redis.
- Google Drive production read path: **DirectGoogleDriveArchiveStore** using Google Drive API v3, a restricted read-only Service Account, and verified local cache.
- Google Drive write/control plane: owner-operated **Google Apps Script** web-app bridge.
- Large annual CSV bytes: direct **Google Drive API resumable upload** by the REMOTE worker to the opaque session URI created by Apps Script.
- Archive durable job/staging/candidate/backup backend: **must be established from the actual REMOTE service environment/config before mutating work**. Legacy Yandex-named files/classes/paths do not prove Yandex Cloud is active.

### Hard boundaries

- Google Drive annual CSV files and dataset-specific coverage registries are the archive source of truth.
- Production historical reads use the direct read-only Google Drive API Service Account path. Apps Script remains the narrow trusted write/mutation control-plane bridge and explicit rollback read transport for resumable-session creation and final verified promotion.
- **Large annual CSV file bytes must not be transported through Apps Script as one base64 JSON POST.**
- **Large annual resumable uploads must not write directly into the existing canonical file.** They upload to a non-canonical staging filename first.
- The Apps Script shared secret is injected server-side on REMOTE. The resumable session URI is a bearer-like capability and must never be logged or returned to users.
- Before any archive write/recovery after the REMOTE migration, perform a read-only backend audit and prove the actual durable backend. If it cannot be proven, fail closed.
- Yandex Object Storage / YDB / Lockbox and legacy `yandex-object-storage` paths are legacy-capable surfaces only unless the current REMOTE runtime explicitly proves otherwise.
- Local HOME/WORK files or chat memory are never authoritative shared state.
- No new runtime provider or primary storage path may be introduced without an explicit architecture change.

## Archive rules

- Archive state is server-owned and shared by every client.
- Annual CSV files and coverage registries live canonically on Google Drive under `MCP архив базы данных`.
- WB weekly-finance coverage uses `reports_registry.csv`; generic datasets such as WB Advertising use `app/registry/dataset_coverage_registry.csv`.

### Canonical refresh contract

The preferred top-level MCP entry for an ordinary request such as “обнови базу данных” is `marketplace_database_update`. The common coordinator is `core/archive_refresh.py`.

An annual archive job is reusable. `COMPLETE` means only that the previous refresh cycle completed successfully; it never means the annual database is permanently final. A later refresh must re-enter dataset-specific discovery/reconciliation even when the previous job is `COMPLETE`.

The mandatory lifecycle is:

`REQUEST -> DISCOVER -> COMPARE COVERAGE -> FETCH/RECONCILE -> NORMALIZE -> MERGE -> VERIFY -> PUBLISH -> COMMIT COVERAGE -> COMPLETE -> POST-CHECK`

A client must not report “database updated” merely because jobs were queued. Every requested job must reach `COMPLETE`, and then `marketplace_database_verify` must pass before success is claimed.

Every archive dataset admitted to the generic update mechanism must declare all of the following:
1. provider discovery rule;
2. coverage/cursor model;
3. exact stable row key;
4. freshness/high-watermark evidence;
5. merge semantics;
6. completion invariants.

Date comparison is mandatory freshness evidence where meaningful, but a maximum date is not a universal identity key. A stronger provider-native identity must remain authoritative when available: for WB weekly finance this is `reportId` at provider/coverage level and `(reportId, rrdId)` at row level; for campaign-day advertising it is `(date, campaign_id)`; event-style datasets use their provider event key/fingerprint.

Two independent protections are required for every refresh:
- **coverage protection** — provider discovery plus canonical registry/coverage state determines which provider units/ranges are missing or correction-eligible;
- **row protection** — canonical annual merge uses the registered stable key so replay cannot create duplicate logical rows and approved corrections can upsert the same grain.

`marketplace_database_verify` is the read-only post-refresh integrity gate. It checks canonical-file presence, date high-watermarks, duplicate/incomplete stable keys, and registry/coverage consistency. A dataset that has not been initialized or cannot prove its required coverage fails closed.

Current registered refresh families are WB finance and WB advertising. A future WB/Ozon archive dataset must register the contract above before `marketplace_database_update(dataset_family="all")` may claim to refresh it. Full details are mirrored in `docs/ARCHIVE_REFRESH_CONTRACT.md`.

### Durable publication

- Queue state and temporary per-report staging must remain in the currently configured durable REMOTE backend so in-flight jobs survive deployments and client disconnects.
- Large-file finalization is split into durable stages:
  1. `PREPARE` builds one immutable annual candidate in the active durable backend and records its size/SHA256.
  2. `UPLOAD_ANNUAL` asks Apps Script to create an official Google Drive resumable session for a **non-canonical staging filename**, then transfers at most one bounded chunk per worker step directly from the REMOTE durable candidate to that session URI.
  3. Google Drive is authoritative for the confirmed byte offset. After interruption, the worker queries the session and uses the returned `Range`; it never assumes all bytes sent were persisted.
  4. If a resumable session expires or becomes unusable, the worker starts a new session from the immutable candidate. Transient failures use bounded exponential backoff with jitter.
  5. After the staged Drive file is complete, exact byte count and Drive `sha256Checksum` must equal the immutable candidate. This verification uses metadata only; the large file is not downloaded through Apps Script.
  6. The same candidate is mirrored byte-for-byte to the configured durable backup path and verified for size.
  7. Only after steps 5-6 pass, Apps Script performs `promote_verified`: it confirms the staged file ID, parent, size and SHA256, renames that verified file to the canonical annual filename, and then trashes the explicitly identified previous canonical file.
  8. Promotion is retry-safe. If execution stops after the rename, the worker can detect an already-promoted canonical file with the expected size/SHA256 and continue without repeating provider download or `PREPARE`.
  9. Only after verified promotion does `COMMIT` update the applicable coverage registry and durable job progress.
- The resumable session URI, confirmed byte offset, staging file identity, previous canonical file identity, candidate identity/checksums and retry state are durable worker state. Session URIs are bearer-like capabilities and must never be emitted to logs or user responses.
- Non-final resumable chunks must be multiples of 256 KiB. The default is 4 MiB; the final chunk may be smaller.
- If the Apps Script bridge does not support the required resumable/promotion controls, the job must fail closed while preserving the candidate and existing progress. It must never fall back to the old large Apps Script/base64 upload or a direct canonical overwrite.
- Legacy restored/Yandex-compatible backups may be migrated to Drive only through an explicitly verified recovery path when Drive lacks the canonical file; their names alone do not make Yandex Cloud current production. Provider data is not re-downloaded for this migration.
- Update is idempotent and registry-driven; already-complete provider reports/requests are not downloaded again.
- WB finance rows are deduplicated by `(reportId, rrdId)` before the annual CSV is written.
- WB Advertising `ads_campaign_daily` rows are deduplicated by `(date, campaign_id)`.
- Partitioning: one logical annual dataset per marketplace / cabinet / dataset / year.
- Annual file pattern: `<cabinet>__<dataset>__<year>.csv`.
- WB weekly finance MAIN uses only `reportType=1` (`Основной`).
- A logical WB week is Monday-Sunday.
- If WB splits one logical week across month/year boundaries, all physical `reportId` fragments belong to that same logical week.
- `reportType=2` (`По выкупам`) is a separate dataset and must never be mixed into MAIN.

## Google Drive access contract

Production archive access intentionally separates read-only and mutating authorization surfaces.

- Historical read-only archive queries use `DirectGoogleDriveArchiveStore` with a restricted Google Service Account and Google Drive API v3.
- Archive writes, resumable-session creation and verified canonical promotion remain controlled by the owner-operated Apps Script bridge.
- Apps Script remains available as an explicit rollback transport for archive reads, but it is not the normal production historical read path.

### Direct Drive API — production read-only archive path

Runtime configuration:
- `MARKETPLACE_MCP_ARCHIVE_DIRECT_GOOGLE=1` — enables the production direct read backend;
- `MARKETPLACE_MCP_ARCHIVE_GOOGLE_CREDENTIAL` — server-side path to the Service Account JSON credential;
- `MARKETPLACE_MCP_ARCHIVE_CACHE_ROOT` — verified local cache root;
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID` — fixed allowed archive root ID.

The Service Account is read-only and is shared only to the archive root required by Marketplaces. The JSON credential stays under the restricted REMOTE secrets boundary and must never be committed or returned to clients.

Downloads use a verified local cache with final files, resumable `.part` files and metadata. A cached object may satisfy an archive read only after its expected size/SHA256 has been verified. If Google Drive is unavailable and no verified cached copy exists, the archive fails closed with `ARCHIVE_SOURCE_UNAVAILABLE`; historical queries must never silently fall back to live WB/Ozon data.

### Apps Script bridge — write/control plane

Runtime configuration:
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_URL` — non-secret `/exec` URL of the deployed web app;
- `MARKETPLACE_MCP_GOOGLE_DRIVE_BRIDGE_SECRET` — shared secret injected server-side on REMOTE;
- `MARKETPLACE_MCP_ARCHIVE_DRIVE_ROOT_ID` — expected fixed archive root ID.

The bridge remains the write/control plane for small writes, resumable-session creation, diagnostic cleanup, and verified staging-to-canonical promotion under the fixed archive root. Its read/status operations remain available only as an explicit rollback transport; production historical reads use the Direct Drive API. For `resumable_start`, Apps Script uses `ScriptApp.getOAuthToken()` only inside Google to start the official Drive upload session and returns the opaque session URI; the Google access token itself never leaves Apps Script.

`promote_verified` is deliberately narrow: it accepts an explicit staged file ID, explicit previous canonical file ID, target folder, canonical filename, expected byte count and expected SHA256. It verifies the staged file before renaming it and only then trashes the old canonical file. This prevents a partially uploaded or wrong file from replacing the working archive.

### Direct Drive API — large annual CSV bytes

Runtime configuration:
- `MARKETPLACE_MCP_DRIVE_RESUMABLE_CHUNK_BYTES` — optional chunk size; must be a multiple of 256 KiB; default 4 MiB.

The REMOTE worker stores the opaque Drive session URI only in the active durable job state and sends at most one chunk per worker step directly to that URI. Subsequent Drive resumable `PUT` requests use the session URI returned by Google; the runtime does not hold a Google refresh token. On interruption the worker queries Google for the confirmed offset before continuing. Expired sessions are restarted from the immutable candidate rather than replaying provider acquisition or `PREPARE`.

## WB Advertising

The current advertising scope is **Wildberries only**. Ozon advertising is not part of this phase.

### Live M0

M0 remains deliberately read-only:

`named WB cabinet -> dedicated Promotion credential -> live active campaigns -> /adv/v3/fullstats -> normalized advertising-attribution metrics`

Server tools:
- `wb_ads_list_active_campaigns` — live WB campaigns with status `9` (active);
- `wb_ads_get_campaign_stats` — normalized statistics for at most 50 campaign IDs over at most 31 calendar days;
- `wb_ads_audit_active` — audits every currently active campaign over the last 7 full Europe/Moscow calendar days by default.

Advertising credentials are a separate logical credential service named `wb_ads`. Promotion-scoped secrets are stored only in the restricted server-side REMOTE secret boundary and must not be stored on Google Drive or in GitHub.

The metric contract is `wb_ads_m0.v1`; its data class is `advertising_attribution_operational`. It includes provider-attributed spend/orders/order amount and calculated CTR, CPC, click-to-order conversion, CPO, order-based DRR and ROAS. These values **must not be presented as actual business profit**. Actual profitability requires separately approved joins to real orders/sales/buyouts, returns, finance and unit economics.

The current WB fullstats live contract accepts at most 50 campaign IDs and a 31-day window. Interactive M0 fails closed rather than returning a partial audit when the active campaign set exceeds one request.

### Advertising Archive V1

Canonical Drive path: `База данных/WB/<cabinet>/<year>/advertising/...`.

Advertising Archive V1 now has canonical annual datasets and generic coverage records. Registered datasets include:
- `ads_campaign_roster_snapshots`;
- `ads_campaign_daily`;
- `ads_product_daily`;
- `ads_search_cluster_daily`;
- `ads_campaign_snapshots`;
- `ads_expenses`;
- `ads_payments`.

`dataset_coverage_registry.csv` records COMPLETE/PASS or COMPLETE/PASS_WITH_FLAGS request coverage. For the first Semantic Core advertising capability, a closed historical cabinet-level query is executable only when:
1. the relevant campaign-roster coverage is complete for the requested period;
2. every roster campaign eligible for fullstats has complete `ads_campaign_daily` request coverage for the entire requested period;
3. the canonical annual files exist.

If any part is missing, the query fails closed instead of returning partial advertising totals.

Semantic Core V1 uses `ads_campaign_daily` only at cabinet level. A product or `nm_id` question must **not** receive cabinet totals; it fails closed until `ads_product_daily` has its own approved semantic contract. Current-day/current-state advertising remains live and is not inferred from the closed historical archive.

### Advertising safety

WB has campaign-control operations implemented as HTTP GETs. HTTP verb does not determine MCP safety. Start, pause and stop are `WRITE`; delete is `DESTRUCTIVE`. Dedicated write/control business tools are not accepted in M0 and will be added only after a separate safety-reviewed phase.

## Semantic Core

The Semantic Core is wired into runtime through `marketplace_business_query` and preserves the original natural-language question as the primary intent signal. Legacy `metric` remains compatibility-only and must not override the user's wording.

### User-facing response policy

High-level MCP tools expose plain Russian `user_message`, `user_reason`, and `user_note` fields for ordinary ChatGPT/Codex answers. Internal routing vocabulary such as `CANONICAL_ARCHIVE`, source-family/status codes, executor names, `fail-closed`, and forbidden-substitute diagnostics remains available in technical fields but must not be repeated in normal user-facing text unless explicitly requested. When historical data cannot be read, the default wording is plain Russian: there is no access to the historical database/data for the requested shop or period. Routine boilerplate such as «данные не подменял» or «обходные способы не использовал» is intentionally omitted from ordinary answers.

The query path now has an explicit source-independent normalization step before source selection. `core/business_query_parser.py` extracts requested measure, grouping, period hint and filter hints. It does **not** choose marketplace fields or APIs; that remains the Semantic Core planner/resolver responsibility.

Canonical components:
- `core/business_query_parser.py` — normalized business dimensions before source selection;
- `core/semantic_registry.yaml` — audited base semantic catalog;
- `core/semantic_registry_extensions.yaml` — validated additive domain registry, currently WB Advertising;
- `core/semantic_intents.yaml` — deterministic natural-language routing and operational business-metric registry;
- `core/semantic_resolver.py` — fail-closed resolver;
- `core/semantic_execution.yaml` — approved weekly-finance executable contracts;
- `core/semantic_archive.py` — coverage-gated weekly-finance archive execution;
- `core/semantic_advertising.py` — coverage-gated advertising archive execution;
- `core/semantic_current_stock.py` — seller-aware live execution of the current WB stock snapshot;
- `core/semantic_business_router.py` — domain-aware dispatch before the legacy router.

### Operational business metrics

Four operational business metrics are currently registered:

- `ORDERS` — WB Statistics Orders, data class `PRELIMINARY_OPERATIONAL`. Ordinary order questions, including **today**, use this source. It is not the complete marketplace order flow and must not answer explicit “all orders / complete order flow” questions.
- `CURRENT_STOCK` — stock physically stored on Wildberries warehouses, data class `CURRENT_OPERATIONAL_STOCK`. The primary source is the current Seller Analytics stocks endpoint; Base-token cabinets may use the official asynchronous warehouse-remains report fallback.
- `CURRENT_FBS_STOCK` — current stock on seller-owned WB warehouses, data class `CURRENT_SELLER_WAREHOUSE_STOCK`. It resolves seller warehouses through `GET /api/v3/warehouses` and inventory through the reviewed read-only `POST /api/v3/stocks/{warehouseId}`. It must never be substituted with `CURRENT_STOCK`.
- `CURRENT_SELLING_PRICE` — current seller-side price. For WB the `price`, `discountedPrice` and `clubDiscountedPrice` fields from `/api/v2/list/goods/filter` are already expressed in the provider currency's major units. When `currencyIsoCode4217=RUB`, the values are rubles and must never be divided by 100.

The generic current-state rule remains a fail-closed fallback. A concrete operational metric may outrank it only when that metric has an explicitly approved source. Therefore “Сколько заказов сегодня?” can resolve to `ORDERS`, while “Какая комиссия сегодня?” remains fail-closed without an approved live commission contract.

All current price/stock metrics are present-state only. A past-date request must fail **before** provider execution and require a separately approved historical source/contract. The server must never substitute today's snapshot for historical truth.

### 92-column weekly-report completion status

The canonical `wb_weekly_finance_main` header has **92 physical columns and all 92 have semantic definitions**. Each field has a documented meaning, role and safe use; the semantic registry fails closed if the physical field set and semantic field catalog diverge. Therefore the original semantic task for the weekly realization report is COMPLETE.

Approved runtime calculations are an additional layer, not a requirement for semantic completeness. Identifiers, flags, historical attributes, percentages and legacy fields must not be turned into totals merely because they exist in the report.

### Approved archive execution

All archive execution requires exact `FULL_COVERAGE` from the relevant COMPLETE coverage registry plus presence of the canonical annual file.

Weekly-finance capabilities:
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

Advertising capability:
- `advertising_performance` — closed historical cabinet-level WB campaign analytics from canonical `ads_campaign_daily`, guarded by roster + fullstats coverage and the `wb_ads_m0.v1` formula contract.

Sales/returns use `saleDt` and explicit `docTypeName` buckets, with Продажа minus Возврат for the approved `retailAmount` / `quantity` calculation. Logistics keeps `deliveryService` and `rebillLogisticCost` separate. Deductions keep `deduction` and `additionalPayment` separate and are never silently netted. Monetary WB reward uses `vw` and `vwNds`; it is not derived from `commissionPercent/kvw/kvwBase`. Weekly `acquiringFee` is `PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING`, not the final monthly acquiring expense.

Historical fulfillment observations use `deliveryMethod`, `officeName` and `rrDate`; their data class is `HISTORICAL_OBSERVED_FULFILLMENT` and they never confirm current fulfillment configuration. Historical warehouse tariff context uses `dlvPrc`, `fixTariffDateFrom`, `fixTariffDateTo`, `warehouseLogisticsCoeff` and `officeName`; its data class is `HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT` and it never confirms the current live warehouse tariff.

Advertising archive metrics retain data class `ADVERTISING_ATTRIBUTION_OPERATIONAL`. DRR/ROAS and attributed orders are advertising-attribution metrics; they are not total seller revenue, the complete marketplace order flow or business profitability.

## Hard source boundaries

The weekly realization archive is not authoritative for the complete marketplace order funnel, current stock, current fulfillment configuration, current live tariffs, detailed storage drivers, detailed acceptance operations, or Ozon data. Those concepts require another approved source and must fail closed instead of being inferred from weekly rows.

Current stock now has that separate approved live source and therefore does not read weekly finance. Historical stock still has **no approved historical source** and remains fail-closed.

Advertising performance is no longer inferred from weekly finance: it has its own registered canonical datasets and executor. Product-level advertising remains unsupported by the first semantic capability and must fail closed until `ads_product_daily` is approved.

WB Statistics Orders remains operational/preliminary (`PRELIMINARY_NOT_ALL_ORDERS`) and must not be substituted for the complete order flow.

## Routing rules

- “Обнови данные по базе данных” and equivalent intents route to `marketplace_database_update` for all requested registered dataset families/cabinets. A client waits for every job to reach `COMPLETE` and then calls `marketplace_database_verify`; queue acceptance alone is never reported as a successful update.
- Natural business questions preserve the user's original wording, normalize source-independent business dimensions, and resolve through Semantic Core before source selection.
- A specific approved operational business metric outranks the generic current-state guard only for its registered source; the generic rule otherwise remains fail-closed.
- Ordinary `ORDERS` questions including today use WB Statistics Orders; explicit complete-order-flow questions remain separate.
- `CURRENT_STOCK` uses the current WB-warehouse snapshot. Explicit FBS/seller-warehouse wording uses `CURRENT_FBS_STOCK` instead; the two stock classes are never interchangeable.
- `CURRENT_SELLING_PRICE` uses the normalized provider price contract; WB RUB fields are rubles and are never divided by 100.
- Historical sales/buyouts/returns/finance questions read the canonical Google Drive archive when semantic approval and exact coverage exist; a callable live API must not replace an available archive source.
- Current/uncovered periods use a suitable provider API or an explicit backfill/gap workflow; no partial archive is silently substituted.
- Complete-order questions do not substitute WB Statistics Orders for the full marketplace order flow.
- Current tariff questions do not use historical weekly-report coefficients as live tariff truth.
- Advertising current campaign state/control is live from WB Promotion API.
- Closed cabinet-level Advertising questions are archive-first only with proven roster/fullstats `FULL_COVERAGE`.
- Product-level advertising questions fail closed until `ads_product_daily` receives a separate semantic contract.
- All computers/chats see the same remote state; no client may invent its own storage or architecture path.

## Change control

Any architecture change must update all of the following in one change:
1. `core/system_map.py` — `SYSTEM_MAP` and `SYSTEM_INSTRUCTIONS`;
2. this document;
3. `AGENTS.md` when a hard storage/deployment/semantic boundary changes;
4. guardrail tests;
5. CI/security/deployment acceptance.

If an implementation conflicts with this map, fail closed and surface the conflict instead of silently changing architecture.
