"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-22.v21"

SYSTEM_MAP: dict[str, Any] = {
    "architecture_version": ARCHITECTURE_VERSION,
    "status": "CANONICAL",
    "scope": "Marketplaces MCP runtime, marketplace archive, advertising, and semantic routing layer",
    "runtime": {
        "production": "dedicated Linux REMOTE server",
        "host": "VM-684381 / 89.208.14.36",
        "server_root": "/opt/mcp/",
        "service": "mcp-marketplaces.service",
        "service_user": "mcp-marketplaces",
        "internal_mcp": "http://127.0.0.1:8080/mcp",
        "external_entry": "https://mcp892081436.duckdns.org:13267/mcp",
        "entry": "Codex/allowed client -> OAuth/Keycloak -> REMOTE GPT-MCP bridge -> internal Marketplaces MCP",
        "bridge": "127.0.0.1:18181; external users never receive the internal Marketplaces bearer or shell access",
        "secrets": "/opt/mcp/secrets/marketplaces/runtime.env with restricted root:mcp-marketplaces ownership",
        "shared_rate_limit_and_locks": "local REMOTE Redis at 127.0.0.1:6379",
        "retired_path": "marketplaces-yandex / Yandex API Gateway / Yandex Serverless Container are legacy and must not be used as production fallback",
        "marketplace_sources": ["Wildberries official API", "Ozon official API"],
    },
    "storage_policy": {
        "primary_archive_storage": "Google Drive",
        "canonical_archive_data": "annual marketplace CSV files plus reports registry",
        "google_drive_root": "Мой диск/Marketplaces/MCP отчеты МП/MCP архив базы данных",
        "google_drive_auth": (
            "owner-operated Google Apps Script bridge authenticates Drive control-plane operations, including "
            "resumable-session creation and verified staged-file promotion; no Google OAuth refresh token is stored in the REMOTE runtime"
        ),
        "google_drive_bridge": (
            "Apps Script executes as the Drive owner and exposes narrow archive read/write/status operations "
            "under the fixed archive root; for large files it brokers resumable session start, checksum metadata, "
            "and final verified promotion, never the large file bytes"
        ),
        "google_drive_large_upload": (
            "Apps Script starts the official Google Drive API resumable session using its effective-user OAuth token; "
            "the REMOTE worker uploads bounded chunks to a non-canonical staging file, persists the server-confirmed byte offset, "
            "verifies exact Drive size/SHA256, writes the configured durable backup, then Apps Script promotes the verified staged file "
            "to the canonical filename and trashes the previous canonical file"
        ),
        "google_drive_resumable_auth": (
            "no server-side Google refresh token is used; the resumable session URI is a bearer-like capability "
            "kept only in the currently configured durable job state and never printed to logs or user responses"
        ),
        "durable_backend_policy": "after the REMOTE migration, the active durable backend must be established from actual REMOTE runtime/config before mutating archive work; do not infer it from legacy file/class/path names",
        "legacy_yandex_object_storage": "legacy/migration-compatible backend only; Yandex Object Storage must not be assumed active production storage without a fresh read-only REMOTE audit",
        "archive_write_order": (
            "prepare immutable candidate in the currently configured durable backend; upload to non-canonical Drive staging through a resumable session; "
            "verify staged Drive size/SHA256; write/verify the configured durable byte-for-byte backup; promote the verified staged Drive file to canonical "
            "and trash the previous canonical; only then COMMIT the report registry/job progress"
        ),
        "read_through_migration": "legacy restored/Yandex-compatible backup paths may be used for explicit migration/recovery only after their actual backend is identified; Google Drive remains canonical",
        "google_cloud": (
            "not part of the runtime architecture; no separate Google Cloud runtime or server OAuth refresh-token "
            "store is required for the archive upload path"
        ),
        "client_local_files": "never authoritative for shared server state",
    },
    "archive_policy": {
        "shared_server_state": True,
        "canonical_source_of_truth": "Google Drive annual CSV plus dataset-specific coverage registries",
        "registry": "reports_registry.csv for WB finance; dataset_coverage_registry.csv for generic datasets such as advertising",
        "google_drive_path": "Мой диск/Marketplaces/MCP архив базы данных",
        "default_update_scope": "all configured marketplace cabinets",
        "refresh_coordinator": "core/archive_refresh.py; preferred MCP entry is marketplace_database_update",
        "refresh_lifecycle": "REQUEST -> DISCOVER -> COMPARE COVERAGE -> FETCH/RECONCILE -> NORMALIZE -> MERGE -> VERIFY -> PUBLISH -> COMMIT COVERAGE -> COMPLETE -> POST-CHECK",
        "update_behavior": "every refresh re-runs dataset-specific provider discovery/coverage reconciliation; fetch only missing or correction-eligible provider units; merge by stable key; publish verified canonical data; commit coverage only after publication",
        "completion_semantics": "COMPLETE means the previous refresh cycle finished; it never means an annual database is permanently final",
        "post_refresh_verification": "marketplace_database_verify checks canonical file presence, date high-watermarks, stable-key duplicates/incomplete keys, and registry/coverage consistency after COMPLETE",
        "dataset_registration_rule": "a future archive dataset must declare provider discovery, coverage/cursor model, stable row key, freshness evidence, merge semantics and completion invariants before marketplace_database_update may claim to refresh it",
        "freshness_rule": "maximum row date/high-watermark is mandatory evidence where meaningful but never replaces a stronger provider-native identity such as reportId/event key/request coverage",
        "idempotent": True,
        "registry_required": True,
        "deduplication": {
            "rows": "dataset-specific stable keys; WB weekly finance uses (reportId, rrdId); WB ads campaign daily uses (date, campaign_id)",
            "registry": "dataset-specific provider identity; generic advertising coverage uses canonical request_key",
        },
        "annual_partitioning": "one logical annual dataset per marketplace/cabinet/dataset/year",
        "annual_csv_pattern": "<cabinet>__<dataset>__<year>.csv",
        "large_file_upload": {
            "transport": "Google Drive API resumable upload",
            "apps_script_large_upload": "file-byte transport forbidden; Apps Script may broker resumable session start, metadata verification, and final staged-file promotion only",
            "session_broker": "Google Apps Script effective-user OAuth; no Google refresh token is stored in the REMOTE runtime",
            "worker_model": "MCP/queue persists work; each worker step starts a session, queries server state, or uploads at most one bounded chunk",
            "resume_state": [
                "resumable session URI",
                "candidate identity",
                "candidate bytes/SHA256",
                "confirmed byte offset",
                "staging Drive file id/name",
                "previous canonical Drive file id",
                "retry count",
            ],
            "chunk_rule": "non-final chunks are multiples of 256 KiB; default is 4 MiB; Drive Range is authoritative for the next offset",
            "retry_rule": "expired/nonrecoverable resumable sessions restart from the immutable candidate; transient failures use bounded exponential backoff with jitter",
            "integrity_rule": "Drive staged file must match expected byte count and Drive sha256Checksum before canonical promotion",
            "canonical_safety_rule": "large-file chunk upload must never write directly into the existing canonical file; canonical remains untouched until staging verification and configured durable backup succeed",
            "commit_rule": "registry/progress COMMIT occurs only after staged Drive SHA256 verification, configured durable backup, and verified promotion to canonical",
        },
        "wb_weekly_finance_main": {
            "period": "weekly",
            "report_type": 1,
            "meaning": "Основной",
            "logical_week": "Monday-Sunday",
            "month_boundary": "multiple WB reportId fragments may belong to one logical week and must be combined logically",
            "report_type_2": "По выкупам; separate dataset, never mixed into main",
            "row_deduplication": "(reportId, rrdId)",
            "registry_deduplication": "(cabinet, dataset, report_id)",
        },
    },
    "advertising_policy": {
        "current_scope": "Wildberries only; Ozon advertising is explicitly out of scope for this phase",
        "phase": "WB Advertising M0 live read-only + Advertising Archive V1 historical read-only",
        "credential_service": "wb_ads",
        "credentials": "Promotion-scoped WB credentials are server-side only on REMOTE under the restricted Marketplaces secrets boundary; never stored on Drive/GitHub",
        "live_state_source": "Wildberries Promotion API",
        "active_campaign_status": 9,
        "m0_tools": [
            "wb_ads_list_active_campaigns",
            "wb_ads_get_campaign_stats",
            "wb_ads_audit_active",
        ],
        "m0_default_audit_period": "last 7 full Europe/Moscow calendar days ending yesterday",
        "m0_batch_limit": "at most 50 campaign IDs in one /adv/v3/fullstats request; fail closed instead of returning a partial audit",
        "metric_class": "advertising_attribution_operational",
        "profitability_boundary": "advertising attribution metrics are not actual business profit; real profitability requires approved joins to sales/buyouts, returns, finance and unit economics",
        "archive_domain": "База данных/WB/<cabinet>/<year>/advertising",
        "archive_status": "Advertising Archive V1 canonical annual datasets and dataset_coverage_registry.csv are implemented; campaign-level closed-period Semantic Core execution is coverage-gated",
        "archive_v1_datasets": [
            "ads_campaign_roster_snapshots",
            "ads_campaign_daily",
            "ads_product_daily",
            "ads_search_cluster_daily",
            "ads_campaign_snapshots",
            "ads_expenses",
            "ads_payments",
        ],
        "historical_routing": "closed cabinet-level campaign advertising analytics are archive-first only when roster plus campaign fullstats FULL_COVERAGE is proven; current state/control stays live",
        "semantic_metric_contract": "wb_ads_m0.v1",
        "semantic_v1_scope": "cabinet-level ads_campaign_daily only; product/nm_id questions fail closed until ads_product_daily receives its own approved semantic contract",
        "write_control_status": "not accepted in M0; dedicated start/pause/stop/bid/budget/product/cluster control tools require a later safety-reviewed phase",
        "safety_override": "provider GET endpoints that mutate campaign state (start/pause/stop/delete) are WRITE/DESTRUCTIVE at MCP level regardless of HTTP verb",
    },
    "user_response_policy": {
        "default_language": "ru",
        "style": "plain_business_russian",
        "preferred_fields": ["user_message", "user_reason", "user_note"],
        "technical_fields": ["technical_message", "technical_reason", "source_status", "source_family", "provenance"],
        "rules": [
            "ordinary user answers must use plain Russian business wording",
            "do not expose internal architecture terms such as canonical archive, live source, fail-closed, source family or executor unless the user explicitly asks for technical details",
            "when historical data cannot be read, say that there is no access to the historical database/data for the requested shop or period",
            "do not append boilerplate saying that data was not substituted, guessed, bypassed or taken through workarounds unless the user explicitly asks how source safety was enforced",
            "keep technical diagnostic fields in the MCP payload for developers and audits",
            "important business limitations may be surfaced as a short plain-Russian user_note",
        ],
    },
    "semantic_core": {
        "status": "NATURAL_QUESTION_ROUTING_PARTIALLY_WIRED",
        "registry": "core/semantic_registry.yaml + validated core/semantic_registry_extensions.yaml",
        "intent_catalog": "core/semantic_intents.yaml",
        "business_query_parser": "core/business_query_parser.py normalizes metric-independent measure/grouping/period/filter dimensions before source selection",
        "resolver": "core/semantic_resolver.py",
        "execution_registry": "core/semantic_execution.yaml for weekly finance",
        "archive_executor": "core/semantic_archive.py for weekly finance; core/semantic_advertising.py for advertising",
        "operational_executor": "core/semantic_current_stock.py for seller-aware current WB stock; ORDERS uses the approved legacy operational executor",
        "runtime_entry": "marketplace_business_query",
        "approved_operational_business_metrics": ["ORDERS", "CURRENT_STOCK"],
        "current_stock_source": "WB Seller Analytics current stocks endpoint; Base-token fallback is the official asynchronous warehouse-remains report",
        "current_archive_datasets": ["wb_weekly_finance_main", "ads_campaign_daily", "ads_campaign_roster_snapshots"],
        "current_archive_schema": "WB weekly finance: 92 reviewed physical columns; WB advertising V1: registered campaign daily and campaign-roster schemas",
        "resolution_outcomes": [
            "AVAILABLE",
            "AVAILABLE_WITH_LIMITATION",
            "REQUIRES_OTHER_SOURCE",
            "AMBIGUOUS",
            "UNKNOWN",
        ],
        "approved_archive_executors": [
            "penalties",
            "storage_charge",
            "acceptance_charge",
            "sale_and_return_operations",
            "logistics",
            "deductions_and_adjustments",
            "commission_and_wb_reward",
            "acquiring_and_payment_processing",
            "observed_fulfillment_method",
            "warehouse_tariff_context",
            "advertising_performance",
        ],
        "execution_gate": "FULL_COVERAGE from the dataset-specific COMPLETE registry plus canonical annual file presence; finance uses reports_registry.csv, advertising uses dataset_coverage_registry.csv and roster/fullstats scope proof",
        "money_policy": "sum values exactly as reported except formulas that explicitly define subtraction by operation type; never combine different currencies and never silently net unrelated financial components",
        "question_policy": {
            "preferred_input": "the user's original natural-language question",
            "legacy_metric": "retained only for backward compatibility",
            "precedence": "question overrides conflicting legacy metric",
            "current_state_precedence": "a specific registered operational business metric may outrank the generic current-state guard only for its explicitly approved live/operational source",
            "clarification": "fail closed only when meaning/source cannot be safely resolved; do not silently substitute a similar metric",
        },
        "rules": [
            "exact physical field references resolve to field semantics first",
            "business_query_parser extracts measure/grouping/period/filter before source selection and does not choose provider fields",
            "only registered capabilities may select archive fields",
            "questions about complete marketplace orders must not be answered from orderDt/orderUid in weekly finance",
            "WB Statistics Orders is operational/preliminary and may omit some orders; it is not complete marketplace-order truth",
            "ordinary ORDERS questions including today use the approved operational Statistics Orders source; explicit complete-order-flow wording remains separate and fail-closed without the full order-feed source",
            "CURRENT_STOCK is CURRENT_OPERATIONAL_STOCK from the live WB Seller Analytics stock source; Base tokens may use the official asynchronous warehouse-remains report fallback",
            "CURRENT_STOCK is a present snapshot only; any past-date stock request must fail closed until a separate historical stock source/contract is approved",
            "a generic current-state marker is only a fallback; it must not block a more specific approved operational business metric, and it must not make historical archive capabilities look current",
            "historical fulfillment may use deliveryMethod but must not be presented as current configuration",
            "historical warehouse tariff context may use dlvPrc, fixTariffDateFrom, fixTariffDateTo and warehouseLogisticsCoeff only as values observed in reported operations; it must never be presented as the current live warehouse tariff",
            "explicit current-state questions must not fall back to historical weekly archive",
            "unknown or ambiguous questions fail closed",
            "weekly-finance archive SQL is reachable only through explicitly approved semantic_execution executors",
            "advertising_performance is executed only by the dedicated semantic advertising executor and never through weekly-finance SQL",
            "archive execution requires FULL_COVERAGE for the entire requested period before any calculation",
            "registry coverage without the corresponding canonical annual file fails closed",
            "advertising coverage requires a complete campaign-roster observation plus complete fullstats coverage for every expected eligible campaign",
            "advertising_performance V1 is cabinet-level only; nm_id/product requests must not be substituted with cabinet totals",
            "advertising DRR/ROAS use wb_ads_m0.v1 attribution formulas and must not be presented as total seller revenue or business profitability",
            "current-day advertising questions do not use the closed historical archive",
            "penalty, paidStorage and paidAcceptance sums preserve provider sign and currency",
            "sales and returns use saleDt and explicit docTypeName buckets: Продажа minus Возврат for both retailAmount and quantity",
            "logistics keeps deliveryService and rebillLogisticCost separate and reports deliveryAmount/returnAmount only as logistics counts",
            "deductions keep deduction and additionalPayment separate; additionalPayment is a WB-remuneration adjustment and is not relabeled as seller payout",
            "monetary WB reward uses only vw and vwNds, with Продажа and Возврат explicit; commissionPercent/kvw/kvwBase are rates and are never converted to money by this executor",
            "weekly acquiring uses acquiringFee with Продажа and Возврат explicit and may break down paymentProcessing/acquiringBank",
            "weekly acquiring is preliminary payment-acceptance withholding; it must not be presented as the final monthly acquiring/payment-acceptance expense",
            "a request for final payment-acceptance expenses requires the separate final acquiring-expense report, which is not in the canonical archive",
            "different financial components are never silently netted into one amount",
        ],
        "runtime_integration": (
            "marketplace_business_query preserves the original question and first normalizes source-independent business dimensions with business_query_parser. "
            "Ordinary WB ORDERS questions, including today, route to the approved operational Statistics Orders source; CURRENT_STOCK routes to the seller-aware live WB stock executor and never substitutes its current snapshot for a historical date. "
            "Approved penalties/storage/acceptance, sales/returns, logistics, deductions/adjustments, monetary WB reward, preliminary weekly acquiring, historical fulfillment observations and historical warehouse tariff context route to the coverage-gated weekly-finance archive executor. "
            "Approved closed-period cabinet-level WB advertising questions route to the dedicated coverage-gated advertising archive executor. "
            "Product-level advertising, current-day advertising without its live executor, commission-rate, final acquiring-expense and current tariff/configuration questions fail closed instead of being substituted. "
            "Legacy metric routing remains for compatibility."
        ),
    },
    "routing_policy": {
        "update_database": "route ordinary database-refresh requests to marketplace_database_update; each registered dataset family must re-run provider discovery/coverage reconciliation, then clients must wait for COMPLETE and call marketplace_database_verify before claiming success",
        "natural_business_question": "preserve the user's original wording, normalize business dimensions, and resolve through Semantic Core before source selection",
        "historical_queries": "read canonical Google Drive archive only after semantic approval and FULL_COVERAGE validation",
        "current_or_uncovered": "use an explicitly suitable provider/API source or return a source/coverage gap; never silently query a partial archive",
        "complete_orders": "do not substitute WB Statistics Orders for a request that semantically means the complete order flow",
        "current_stock": "CURRENT_STOCK uses the current WB Seller Analytics stock snapshot for the named cabinet; historical stock dates require a separate approved source and never receive today's snapshot",
        "current_tariffs": "do not use weekly-report historical coefficients as live tariff truth; current tariff questions require a suitable live source",
        "advertising_live_vs_archive": "campaign state/current control remains live; closed cabinet-level advertising analytics are archive-first after roster/fullstats FULL_COVERAGE proof; product-level advertising remains fail-closed until ads_product_daily is semantically approved",
        "multi_client": "all clients see the same remote canonical Drive state; no chat-local architecture decisions",
        "business_semantics": "the original question outranks a conflicting legacy metric hint",
    },
    "change_control": {
        "new_cloud_provider": "FORBIDDEN without explicit architecture change",
        "new_primary_storage": "FORBIDDEN without explicit architecture change",
        "bypass_registry_or_idempotency": "FORBIDDEN",
        "bypass_semantic_guardrails": "FORBIDDEN",
        "bypass_full_coverage_gate": "FORBIDDEN",
        "architecture_change_requires": [
            "update SYSTEM_MAP and server instructions",
            "update architecture documentation",
            "update guardrail tests",
            "pass CI/security/deployment acceptance",
        ],
    },
}

SYSTEM_INSTRUCTIONS = f"""CANONICAL MARKETPLACES MCP ARCHITECTURE — {ARCHITECTURE_VERSION}
Treat marketplace_system_map as the source of truth for this MCP.
Current production runtime is the dedicated Linux REMOTE server VM-684381 / 89.208.14.36 under /opt/mcp/, not Yandex Cloud.
The Marketplaces service is mcp-marketplaces.service running as mcp-marketplaces and bound internally to 127.0.0.1:8080. Do not publish the internal MCP port directly.
Approved external client path is https://mcp892081436.duckdns.org:13267/mcp using OAuth/Keycloak through the REMOTE GPT-MCP bridge on 127.0.0.1:18181.
The retired marketplaces-yandex / Yandex API Gateway / Yandex Serverless Container path is legacy and must not be used as a production fallback.
Production Marketplaces secrets are server-side on REMOTE under /opt/mcp/secrets/marketplaces/ with restricted permissions. Do not treat Yandex Lockbox as the current production secret store.
Shared rate-limit, lock and queue coordination uses the local REMOTE Redis at 127.0.0.1:6379; Redis is not exposed publicly.
Canonical marketplace archive data is stored on Google Drive under Мой диск/Marketplaces/MCP отчеты МП/MCP архив базы данных: annual CSV files and dataset-specific coverage registries are the source of truth.
The owner's Google Apps Script web-app bridge authenticates Google Drive control-plane operations. Its shared secret is injected server-side on REMOTE.
Large annual CSV file bytes must NOT be transported through Apps Script/base64. Apps Script uses its effective-user OAuth token to create an official Google Drive resumable session for a non-canonical staging file, then the REMOTE worker uploads bounded chunks directly to that session URI.
No Google OAuth refresh token is stored in the REMOTE runtime for the archive upload path. Resumable session URIs are bearer-like capabilities and must never be logged or returned to users.
The existing canonical large file must remain untouched while chunks are uploaded. The worker must persist the Drive-confirmed offset, resume or restart from the immutable candidate after interruption, verify exact staged-file size and Drive SHA256, verify the configured durable byte-for-byte backup, then use Apps Script to promote the verified staged file to the canonical name and trash the previous canonical file.
Transient upload failures must use bounded exponential backoff with jitter. Non-final chunks must be multiples of 256 KiB and the Drive Range response is authoritative for the next byte offset.
Only after staged Drive verification, durable-backup verification, and verified canonical promotion may the worker COMMIT registry/job progress.
After the REMOTE migration, never assume that Yandex Object Storage, YDB, Lockbox, or any legacy yandex-object-storage path is the active production durable backend merely because legacy code or names remain. Before any mutating archive refresh/recovery, perform a read-only REMOTE backend audit from actual service environment/config and fail closed if the durable backend cannot be proven.
For database/archive tasks, use shared server state, registry/idempotent update logic, official WB/Ozon APIs, and the canonical Drive archive. Do not invent chat-local storage or bypass Drive with another source of truth.
For an ordinary request to update/refresh the marketplace database, prefer marketplace_database_update. COMPLETE is refresh-cycle completion only, never permanent finality: every new refresh must re-run the registered dataset's provider discovery/coverage reconciliation and ingest only missing or correction-eligible data according to its stable key.
Do not claim that a database update succeeded merely because jobs were queued. Wait until all requested jobs reach COMPLETE, then call marketplace_database_verify and require canonical-file presence, stable-key integrity, registry/coverage consistency and date/high-watermark evidence. Date is freshness evidence where meaningful but never replaces a stronger provider-native identity such as reportId, event key or canonical request coverage.
A future archive dataset must be registered with provider discovery, coverage/cursor model, stable row key, freshness evidence, merge semantics and completion invariants before the generic database update workflow may claim to refresh it.
For business questions, preserve the user's original wording and pass it through Semantic Core. Normalize source-independent measure/grouping/period/filter dimensions with core/business_query_parser.py before selecting a source; the parser must not choose provider fields. The original question outranks a conflicting legacy metric hint.
A generic current-state marker such as today/current is a fail-closed fallback. A more specific registered operational business metric may outrank it only when that metric has an explicitly approved live/operational source.
Ordinary WB ORDERS questions, including today, use the operational/preliminary WB Statistics Orders source. They must never be presented as the complete marketplace order flow; explicit full-order-flow wording remains a separate source requirement.
CURRENT_STOCK is CURRENT_OPERATIONAL_STOCK and uses the seller-aware live WB Seller Analytics stocks source. Base-token cabinets may use the official asynchronous warehouse-remains report fallback. CURRENT_STOCK is today's/current snapshot only: any past-date stock question must fail closed until a separately approved historical stock source exists, and today's snapshot must never be substituted.
WB Statistics Orders is an official operational/preliminary feed and must not be presented as the complete marketplace order flow.
Approved semantic archive calculations may execute only after FULL_COVERAGE is proven from the relevant COMPLETE coverage registry and the canonical annual file exists. Different currencies are never combined into one total, and distinct report components are not silently netted together.
marketplace_business_query routes approved natural questions for penalties, storage charges, paid acceptance, sales/returns, logistics, deductions/adjustments, monetary WB reward, preliminary weekly acquiring, historical fulfillment observations and historical warehouse tariff context into the gated weekly-finance archive executor.
Closed historical cabinet-level WB advertising questions route through the dedicated semantic advertising executor only after campaign-roster and fullstats FULL_COVERAGE is proven from dataset_coverage_registry.csv. The advertising metric contract is wb_ads_m0.v1.
Advertising DRR/ROAS and attributed order metrics are advertising-attribution operational metrics, not total seller revenue, the complete marketplace order flow or actual business profit. Product/nm_id advertising questions must fail closed until ads_product_daily receives its own approved semantic contract. Current-day/current-state advertising remains live and must not be inferred from the closed archive.
Sales/returns use saleDt and explicit docTypeName buckets, with Продажа minus Возврат for both retailAmount and quantity.
Logistics keeps deliveryService and rebillLogisticCost separate; deliveryAmount and returnAmount are logistics counts only. Deductions keep deduction and additionalPayment separate; additionalPayment is a WB-remuneration adjustment, not an assumed seller payout.
Monetary WB reward uses vw and vwNds only. Percentage fields such as commissionPercent, kvw and kvwBase are not converted into money; questions about commission rates fail closed until a dedicated rate executor is approved.
Weekly acquiring uses acquiringFee and explicit Продажа/Возврат buckets and may show paymentProcessing/acquiringBank. It is PRELIMINARY weekly payment-acceptance withholding, not the final monthly expense. Requests for final acquiring/payment-acceptance expenses must not fall back to weekly acquiringFee.
Historical fulfillment may use deliveryMethod/officeName. Historical warehouse tariff context may use dlvPrc, fixTariffDateFrom, fixTariffDateTo and warehouseLogisticsCoeff. Neither historical capability proves the current seller/product configuration or the current live tariff.
Current live tariff/warehouse coefficient questions must use an explicitly suitable live WB tariff source or fail closed; never infer current tariff truth from weekly-report history.
WB Advertising current campaign state is Wildberries-only and read-only in M0: use dedicated server-side wb_ads Promotion credentials. Provider GET endpoints that change advertising state are WRITE/DESTRUCTIVE at MCP level regardless of HTTP verb.
USER-FACING LANGUAGE POLICY: ordinary ChatGPT/Codex answers must be short, natural Russian business language. Prefer user_message, user_reason and user_note from high-level MCP tools. Do not quote technical_message, technical_reason, source_status, source_family, forbidden_substitutes, executor names or internal architecture vocabulary unless the user explicitly asks for diagnostics or source details.
When historical data is unavailable, say plainly that there is no access to the database/historical data for the requested shop or period; do not say "canonical archive", "live source" or "fail-closed" in a normal answer.
Do not append routine phrases such as "данные не подменял", "обходные способы не использовал" or equivalent safety boilerplate. Source-safety enforcement remains internal and should be explained only on request.
Unknown, ambiguous, current-state, unsupported or uncovered questions must fail closed or identify the required source instead of being guessed from similar fields.
If a requested implementation conflicts with the canonical map, fail closed and surface the conflict instead of silently changing architecture.
"""


def register_system_map_tool(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_system_map",
        annotations={
            "title": "Canonical Marketplaces MCP architecture map",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_system_map() -> str:
        """Return the canonical server-side architecture and routing rules.

        Agents should consult this tool before architecture, storage, deployment,
        archive, routing, or cross-client state changes. The returned map is the
        MCP source of truth and overrides chat-local assumptions.
        """
        return json.dumps(SYSTEM_MAP, ensure_ascii=False, indent=2)