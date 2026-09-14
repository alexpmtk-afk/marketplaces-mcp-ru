"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-14.v15"

SYSTEM_MAP: dict[str, Any] = {
    "architecture_version": ARCHITECTURE_VERSION,
    "status": "CANONICAL",
    "scope": "Marketplaces MCP runtime, marketplace archive, advertising, and semantic routing layer",
    "runtime": {
        "cloud": "Yandex Cloud only",
        "entry": "ChatGPT/Codex -> marketplaces-yandex -> Yandex API Gateway -> Yandex Serverless Container",
        "secrets": "Yandex Lockbox",
        "shared_rate_limit_and_locks": "Yandex Managed Redis/Valkey",
        "marketplace_sources": ["Wildberries official API", "Ozon official API"],
    },
    "storage_policy": {
        "primary_archive_storage": "Google Drive",
        "canonical_archive_data": "annual marketplace CSV files plus reports registry",
        "google_drive_root": "MCP архив базы данных",
        "google_drive_auth": (
            "owner-operated Google Apps Script bridge authenticates Drive control-plane operations, including "
            "resumable-session creation; no Google OAuth refresh token is stored in Yandex"
        ),
        "google_drive_bridge": (
            "Apps Script executes as the Drive owner and exposes narrow archive read/write/status operations "
            "under the fixed archive root; for large files it brokers only the resumable session start, "
            "not the file bytes"
        ),
        "google_drive_large_upload": (
            "Apps Script starts the official Google Drive API resumable session using its effective-user OAuth token; "
            "Yandex then uploads bounded chunks directly to the returned Drive session URI and persists the "
            "confirmed byte offset durably"
        ),
        "google_drive_resumable_auth": (
            "no server-side Google refresh token is used; the resumable session URI is a bearer-like capability "
            "kept only in durable Yandex job state and never printed to logs or user responses"
        ),
        "yandex_object_storage": "durable archive job state, staging, upload resume state, and byte-for-byte backup of canonical files",
        "archive_write_order": (
            "prepare immutable candidate in Yandex Object Storage; verify resumable Google Drive canonical write; "
            "write byte-for-byte Yandex backup; only then COMMIT the report registry/job progress"
        ),
        "read_through_migration": "if a canonical file is absent on Drive but exists in Yandex Object Storage, copy it to Drive before use",
        "google_cloud": (
            "not part of the runtime architecture; no separate Google Cloud runtime or server OAuth refresh-token "
            "store is required for the archive upload path"
        ),
        "client_local_files": "never authoritative for shared server state",
    },
    "archive_policy": {
        "shared_server_state": True,
        "canonical_source_of_truth": "Google Drive annual CSV plus reports registry",
        "registry": "reports_registry.csv",
        "google_drive_path": "Мой диск/Marketplaces/MCP архив базы данных",
        "default_update_scope": "all configured marketplace cabinets",
        "update_behavior": "compare registry -> request only missing report IDs -> update annual CSV",
        "idempotent": True,
        "registry_required": True,
        "deduplication": {
            "rows": "dataset-specific stable keys; WB weekly finance uses (reportId, rrdId)",
            "registry": "(cabinet, dataset, report_id)",
        },
        "annual_partitioning": "one logical annual dataset per marketplace/cabinet/dataset/year",
        "annual_csv_pattern": "<cabinet>__<dataset>__<year>.csv",
        "large_file_upload": {
            "transport": "Google Drive API resumable upload",
            "apps_script_large_upload": "file-byte transport forbidden; Apps Script may broker resumable session start only",
            "session_broker": "Google Apps Script effective-user OAuth; no Google refresh token is stored in Yandex",
            "worker_model": "MCP/queue persists work; each worker step starts a session or uploads at most one bounded chunk",
            "resume_state": [
                "resumable session URI",
                "candidate identity",
                "candidate bytes/SHA256",
                "confirmed byte offset",
                "target Drive file id/name",
            ],
            "chunk_rule": "non-final chunks are multiples of 256 KiB; v1 default is 4 MiB",
            "commit_rule": "registry/progress COMMIT occurs only after canonical Drive verification and Yandex backup",
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
        "phase": "WB Advertising M0 read-only",
        "credential_service": "wb_ads",
        "credentials": "Promotion-scoped WB credentials are server-side only and must be injected from Yandex Lockbox; never stored on Drive/GitHub",
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
        "archive_status": "Drive folder scaffold exists; ingestion/coverage/registry integration is not yet implemented or accepted",
        "planned_datasets": [
            "ads_campaign_daily",
            "ads_product_daily",
            "ads_search_cluster_daily",
            "ads_campaign_snapshots",
            "ads_expenses",
            "ads_payments",
            "ads_bid_history",
            "ads_product_membership_history",
            "ads_placement_history",
            "ads_minus_phrase_history",
            "ads_mcp_actions",
        ],
        "historical_routing": "when Advertising Archive V1 is implemented and coverage is proven, closed historical periods must be archive-first; current state/control stays live",
        "write_control_status": "not accepted in M0; dedicated start/pause/stop/bid/budget/product/cluster control tools require a later safety-reviewed phase",
        "safety_override": "provider GET endpoints that mutate campaign state (start/pause/stop/delete) are WRITE/DESTRUCTIVE at MCP level regardless of HTTP verb",
    },
    "semantic_core": {
        "status": "NATURAL_QUESTION_ROUTING_PARTIALLY_WIRED",
        "registry": "core/semantic_registry.yaml",
        "intent_catalog": "core/semantic_intents.yaml",
        "resolver": "core/semantic_resolver.py",
        "execution_registry": "core/semantic_execution.yaml",
        "archive_executor": "core/semantic_archive.py",
        "runtime_entry": "marketplace_business_query",
        "current_archive_dataset": "wb_weekly_finance_main",
        "current_archive_schema": "92 physical columns, each with reviewed semantic meaning/safe uses/limitations",
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
        ],
        "execution_gate": "FULL_COVERAGE from COMPLETE reports_registry.csv fragments plus canonical annual file presence",
        "money_policy": "sum values exactly as reported except formulas that explicitly define subtraction by operation type; never combine different currencies and never silently net unrelated financial components",
        "question_policy": {
            "preferred_input": "the user's original natural-language question",
            "legacy_metric": "retained only for backward compatibility",
            "precedence": "question overrides conflicting legacy metric",
            "clarification": "fail closed only when meaning/source cannot be safely resolved; do not silently substitute a similar metric",
        },
        "rules": [
            "exact physical field references resolve to field semantics first",
            "only registered capabilities may select archive fields",
            "questions about complete marketplace orders must not be answered from orderDt/orderUid in weekly finance",
            "WB Statistics Orders is operational/preliminary and may omit some orders; it is not complete marketplace-order truth",
            "historical fulfillment may use deliveryMethod but must not be presented as current configuration",
            "historical warehouse tariff context may use dlvPrc, fixTariffDateFrom, fixTariffDateTo and warehouseLogisticsCoeff only as values observed in reported operations; it must never be presented as the current live warehouse tariff",
            "explicit current-state questions must not fall back to historical weekly archive",
            "unknown or ambiguous questions fail closed",
            "only explicitly approved semantic_execution executors may reach archive SQL",
            "archive execution requires FULL_COVERAGE for the entire requested period before any calculation",
            "registry coverage without the corresponding canonical annual file fails closed",
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
            "marketplace_business_query accepts the original question; approved penalties/storage/acceptance, "
            "sales/returns, logistics, deductions/adjustments, monetary WB reward, preliminary weekly acquiring, "
            "historical fulfillment observations and historical warehouse tariff context route to the coverage-gated archive executor. "
            "Commission-rate questions, final acquiring-expense questions and current tariff/configuration questions fail closed instead of being substituted. "
            "Legacy metric routing remains for compatibility."
        ),
    },
    "routing_policy": {
        "update_database": "route to the server archive update workflow; compare canonical registry and fetch only missing provider reports",
        "natural_business_question": "preserve the user's original wording and resolve it through Semantic Core before source selection",
        "historical_queries": "read canonical Google Drive archive only after semantic approval and FULL_COVERAGE validation",
        "current_or_uncovered": "use an explicitly suitable provider/API source or return a source/coverage gap; never silently query a partial archive",
        "complete_orders": "do not substitute WB Statistics Orders for a request that semantically means the complete order flow",
        "current_tariffs": "do not use weekly-report historical coefficients as current WB tariff truth; current tariff questions require a suitable live source",
        "advertising_live_vs_archive": "campaign state/current control is live; closed advertising analytics becomes archive-first only after the ad dataset binding and coverage are implemented and proven",
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
Runtime infrastructure is Yandex Cloud. Google Cloud is not a runtime provider for Marketplaces MCP.
Canonical marketplace archive data is stored on Google Drive under the server-owned archive root: annual CSV files and the report registry are the source of truth.
Yandex Object Storage is required for durable queue/job state, staging, resumable-upload state, and a secondary byte-for-byte backup of canonical archive files.
The owner's Google Apps Script web-app bridge authenticates Google Drive control-plane operations; its shared secret remains in Yandex Lockbox.
Large annual CSV file bytes must NOT be transported through Apps Script/base64. Apps Script uses its effective-user OAuth token only to create an official Google Drive resumable session, then Yandex uploads bounded chunks directly to that session URI.
No Google OAuth refresh token is stored in Yandex for the archive upload path. Resumable session URIs are bearer-like capabilities and must never be logged or returned to users.
A large-file worker must persist confirmed byte offsets, resume after interruption, verify the canonical Drive result, write the Yandex backup, and only then COMMIT registry/job progress.
For database/archive tasks, use shared server state, registry/idempotent update logic, official WB/Ozon APIs, and the canonical Drive archive. Do not invent chat-local storage or bypass Drive with another source of truth.
For business questions, preserve the user's original wording and pass it through Semantic Core before selecting a source. The original question outranks a conflicting legacy metric hint.
Approved semantic archive calculations may execute only after FULL_COVERAGE is proven from COMPLETE registry fragments and the canonical annual file exists. Different currencies are never combined into one total, and distinct report components are not silently netted together.
marketplace_business_query routes approved natural questions for penalties, storage charges, paid acceptance, sales/returns, logistics, deductions/adjustments, monetary WB reward, preliminary weekly acquiring, historical fulfillment observations and historical warehouse tariff context into the gated archive executor.
Sales/returns use saleDt and explicit docTypeName buckets, with Продажа minus Возврат for both retailAmount and quantity.
Logistics keeps deliveryService and rebillLogisticCost separate; deliveryAmount and returnAmount are logistics counts only. Deductions keep deduction and additionalPayment separate; additionalPayment is a WB-remuneration adjustment, not an assumed seller payout.
Monetary WB reward uses vw and vwNds only. Percentage fields such as commissionPercent, kvw and kvwBase are not converted into money; questions about commission rates fail closed until a dedicated rate executor is approved.
Weekly acquiring uses acquiringFee and explicit Продажа/Возврат buckets and may show paymentProcessing/acquiringBank. It is PRELIMINARY weekly payment-acceptance withholding, not the final monthly expense. Requests for final acquiring/payment-acceptance expenses must not fall back to weekly acquiringFee.
Historical fulfillment may use deliveryMethod/officeName. Historical warehouse tariff context may use dlvPrc, fixTariffDateFrom, fixTariffDateTo and warehouseLogisticsCoeff. Neither historical capability proves the current seller/product configuration or the current live tariff.
Current live tariff/warehouse coefficient questions must use an explicitly suitable live WB tariff source or fail closed; never infer current tariff truth from weekly-report history.
WB Statistics Orders is an official operational/preliminary feed and must not be presented as the complete marketplace order flow.
WB Advertising M0 is Wildberries-only and read-only: use dedicated server-side wb_ads Promotion credentials; current campaign state is live from WB Promotion API; M0 advertising-attribution metrics must never be presented as actual business profit.
The advertising Drive folder scaffold is not proof that Advertising Archive V1 ingestion or historical coverage exists. Do not route historical ad analytics to the archive until dataset bindings, registry coverage and validation are implemented and accepted.
Provider GET endpoints that change advertising state are WRITE/DESTRUCTIVE at MCP level regardless of HTTP verb.
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