"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-14.v7"

SYSTEM_MAP: dict[str, Any] = {
    "architecture_version": ARCHITECTURE_VERSION,
    "status": "CANONICAL",
    "scope": "Marketplaces MCP runtime, marketplace archive, and semantic routing layer",
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
        "google_drive_auth": "owner-operated Google Apps Script web-app bridge; shared bridge secret kept in Yandex Lockbox",
        "google_drive_bridge": "Apps Script executes as the Drive owner and exposes only archive read/write/status operations under the fixed archive root",
        "yandex_object_storage": "durable archive job state, staging, and byte-for-byte backup of canonical files",
        "archive_write_order": "Google Drive canonical write first; Yandex backup second",
        "read_through_migration": "if a canonical file is absent on Drive but exists in Yandex Object Storage, copy it to Drive before use",
        "google_cloud": "not part of the runtime architecture; no Google Cloud OAuth runtime dependency is required",
        "client_local_files": "never authoritative for shared server state",
    },
    "archive_policy": {
        "shared_server_state": True,
        "default_update_scope": "all configured marketplace cabinets",
        "idempotent": True,
        "registry_required": True,
        "annual_partitioning": "one logical annual dataset per marketplace/cabinet/dataset/year",
        "annual_csv_pattern": "<cabinet>__<dataset>__<year>.csv",
        "canonical_source_of_truth": "Google Drive annual CSV plus reports registry",
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
        ],
        "execution_gate": "FULL_COVERAGE from COMPLETE reports_registry.csv fragments plus canonical annual file presence",
        "money_policy": "sum values exactly as reported; never combine different currencies into one total",
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
            "explicit current-state questions must not fall back to historical weekly archive",
            "unknown or ambiguous questions fail closed",
            "only explicitly approved semantic_execution executors may reach archive SQL",
            "archive execution requires FULL_COVERAGE for the entire requested period before any calculation",
            "registry coverage without the corresponding canonical annual file fails closed",
            "penalty, paidStorage and paidAcceptance sums preserve provider sign and currency",
            "sales, commissions and other recognized capabilities remain non-executable until separate formulas are approved",
        ],
        "runtime_integration": (
            "marketplace_business_query accepts the original question; approved penalties/storage/acceptance "
            "route to the coverage-gated archive executor. Other concepts fail closed or identify another "
            "required source. Legacy metric routing remains for compatibility."
        ),
    },
    "routing_policy": {
        "update_database": "route to the server archive update workflow; compare canonical registry and fetch only missing provider reports",
        "natural_business_question": "preserve the user's original wording and resolve it through Semantic Core before source selection",
        "historical_queries": "read canonical Google Drive archive only after semantic approval and FULL_COVERAGE validation",
        "current_or_uncovered": "use an explicitly suitable provider/API source or return a source/coverage gap; never silently query a partial archive",
        "complete_orders": "do not substitute WB Statistics Orders for a request that semantically means the complete order flow",
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
Runtime infrastructure is Yandex Cloud. Google Cloud is not part of the runtime architecture.
Canonical marketplace archive data is stored on Google Drive under the server-owned archive root: annual CSV files and the report registry are the source of truth.
Yandex Object Storage is required for durable queue/job state, staging, and a secondary byte-for-byte backup of canonical archive files.
Google Drive access is provided by the owner's Google Apps Script web-app bridge; its shared secret must remain in Yandex Lockbox.
For database/archive tasks, use shared server state, registry/idempotent update logic, official WB/Ozon APIs, and the canonical Drive archive. Do not invent chat-local storage or bypass Drive with another source of truth.
For business questions, preserve the user's original wording and pass it through Semantic Core before selecting a source. The original question outranks a conflicting legacy metric hint.
Approved semantic archive calculations may execute only after FULL_COVERAGE is proven from COMPLETE registry fragments and the canonical annual file exists. Values are summed exactly as reported and different currencies are never combined into one total.
marketplace_business_query now routes natural questions for penalties, storage charges and paid acceptance into the gated archive executor. Other recognized capabilities remain blocked until their own execution contracts or suitable sources are approved.
WB Statistics Orders is an official operational/preliminary feed and must not be presented as the complete marketplace order flow.
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
