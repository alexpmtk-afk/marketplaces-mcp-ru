"""Canonical architecture map exposed by the MCP server itself."""
from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

ARCHITECTURE_VERSION = "2026-09-14.v5"

SYSTEM_MAP: dict[str, Any] = {
    "architecture_version": ARCHITECTURE_VERSION,
    "status": "CANONICAL",
    "scope": "Marketplaces MCP runtime and marketplace archive",
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
    "routing_policy": {
        "update_database": "route to the server archive update workflow; compare canonical registry and fetch only missing provider reports",
        "historical_queries": "read canonical Google Drive archive for covered periods before repeatedly querying provider APIs",
        "current_or_uncovered": "use provider APIs or explicit gap/backfill workflow",
        "advertising_live_vs_archive": "campaign state/current control is live; closed advertising analytics becomes archive-first only after the ad dataset binding and coverage are implemented and proven",
        "multi_client": "all clients see the same remote canonical Drive state; no chat-local architecture decisions",
    },
    "change_control": {
        "new_cloud_provider": "FORBIDDEN without explicit architecture change",
        "new_primary_storage": "FORBIDDEN without explicit architecture change",
        "bypass_registry_or_idempotency": "FORBIDDEN",
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
WB Advertising M0 is Wildberries-only and read-only: use dedicated server-side wb_ads Promotion credentials; current campaign state is live from WB Promotion API; M0 advertising-attribution metrics must never be presented as actual business profit.
The advertising Drive folder scaffold is not proof that Advertising Archive V1 ingestion or historical coverage exists. Do not route historical ad analytics to the archive until dataset bindings, registry coverage and validation are implemented and accepted.
Provider GET endpoints that change advertising state are WRITE/DESTRUCTIVE at MCP level regardless of HTTP verb.
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
