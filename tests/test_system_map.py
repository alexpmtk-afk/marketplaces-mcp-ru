"""Guardrails for the canonical Marketplaces MCP architecture map."""
from __future__ import annotations

import asyncio
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.system_map import (
    ARCHITECTURE_VERSION,
    SYSTEM_INSTRUCTIONS,
    SYSTEM_MAP,
    register_system_map_tool,
)

ROOT = Path(__file__).resolve().parent.parent


def test_canonical_map_fixes_storage_boundaries():
    assert ARCHITECTURE_VERSION == "2026-09-24.v24"
    assert SYSTEM_MAP["status"] == "CANONICAL"
    runtime = SYSTEM_MAP["runtime"]
    assert runtime["production"] == "dedicated Linux REMOTE server"
    assert runtime["service"] == "mcp-marketplaces.service"
    assert runtime["service_user"] == "mcp-marketplaces"
    assert runtime["internal_mcp"] == "http://127.0.0.1:8080/mcp"
    assert runtime["external_entry"] == "https://mcp892081436.duckdns.org:13267/mcp"
    assert "OAuth/Keycloak" in runtime["entry"]
    assert "127.0.0.1:18181" in runtime["bridge"]
    assert "/opt/mcp/secrets/marketplaces/runtime.env" in runtime["secrets"]
    assert "127.0.0.1:6379" in runtime["shared_rate_limit_and_locks"]
    assert "marketplaces-yandex" in runtime["retired_path"]
    assert "must not be used as production fallback" in runtime["retired_path"]
    storage = SYSTEM_MAP["storage_policy"]
    assert storage["primary_archive_storage"] == "Google Drive"
    assert storage["google_drive_root"] == "Мой диск/Marketplaces/MCP отчеты МП/MCP архив базы данных"
    assert "Apps Script" in storage["google_drive_auth"]
    assert "Service Account" in storage["google_drive_auth"]
    assert "no Google OAuth refresh token" in storage["google_drive_auth"]
    assert "DirectGoogleDriveArchiveStore" in storage["google_drive_read_backend"]
    assert "read-only" in storage["google_drive_read_auth"]
    assert "Service Account" in storage["google_drive_read_auth"]
    assert "SHA256" in storage["google_drive_read_cache"]
    assert "ARCHIVE_SOURCE_UNAVAILABLE" in storage["google_drive_read_cache"]
    assert "Google Drive API" in storage["google_drive_large_upload"]
    assert "resumable" in storage["google_drive_large_upload"]
    assert "effective-user OAuth" in storage["google_drive_large_upload"]
    assert "non-canonical staging" in storage["google_drive_large_upload"]
    assert "SHA256" in storage["google_drive_large_upload"]
    assert "promotes" in storage["google_drive_large_upload"]
    assert "actual REMOTE runtime/config" in storage["durable_backend_policy"]
    assert "before mutating archive work" in storage["durable_backend_policy"]
    assert "must not be assumed active production storage" in storage["legacy_yandex_object_storage"]
    assert "non-canonical Drive staging" in storage["archive_write_order"]
    assert "promote" in storage["archive_write_order"]
    assert "COMMIT" in storage["archive_write_order"]
    assert "not part of the runtime architecture" in storage["google_cloud"]
    assert "no separate Google Cloud runtime" in storage["google_cloud"]
    archive = SYSTEM_MAP["archive_policy"]
    assert archive["canonical_source_of_truth"] == "Google Drive annual CSV plus dataset-specific coverage registries"
    assert "reports_registry.csv" in archive["registry"]
    assert "dataset_coverage_registry.csv" in archive["registry"]
    assert archive["refresh_coordinator"].startswith("core/archive_refresh.py")
    assert "DISCOVER" in archive["refresh_lifecycle"]
    assert "permanently final" in archive["completion_semantics"]
    assert "marketplace_database_verify" in archive["post_refresh_verification"]
    assert "stable row key" in archive["dataset_registration_rule"]
    assert "reportId" in archive["freshness_rule"]
    assert archive["wb_weekly_finance_main"]["report_type"] == 1
    assert archive["wb_weekly_finance_main"]["logical_week"] == "Monday-Sunday"
    assert archive["wb_weekly_finance_main"]["row_deduplication"] == "(reportId, rrdId)"
    assert "none for mutating refreshes" in archive["default_update_scope"]
    assert "explicit" in archive["default_update_scope"]


def test_large_annual_files_use_staged_resumable_drive_api_not_canonical_overwrite():
    policy = SYSTEM_MAP["archive_policy"]["large_file_upload"]
    assert policy["transport"] == "Google Drive API resumable upload"
    assert "file-byte transport forbidden" in policy["apps_script_large_upload"]
    assert "staged-file promotion" in policy["apps_script_large_upload"]
    assert "Apps Script" in policy["session_broker"]
    assert "no Google refresh token" in policy["session_broker"]
    assert "one bounded chunk" in policy["worker_model"]
    assert "confirmed byte offset" in policy["resume_state"]
    assert "staging Drive file id/name" in policy["resume_state"]
    assert "previous canonical Drive file id" in policy["resume_state"]
    assert "retry count" in policy["resume_state"]
    assert "256 KiB" in policy["chunk_rule"]
    assert "Range is authoritative" in policy["chunk_rule"]
    assert "exponential backoff" in policy["retry_rule"]
    assert "sha256Checksum" in policy["integrity_rule"]
    assert "never write directly" in policy["canonical_safety_rule"]
    assert "canonical remains untouched" in policy["canonical_safety_rule"]
    assert "configured durable backup" in policy["commit_rule"]
    assert "promotion to canonical" in policy["commit_rule"]
    assert "COMMIT" in policy["commit_rule"]


def test_wb_advertising_live_and_archive_boundaries_are_canonical():
    policy = SYSTEM_MAP["advertising_policy"]
    assert policy["current_scope"].startswith("Wildberries only")
    assert policy["phase"] == "WB Advertising M0 live read-only + Advertising Archive V1 historical read-only"
    assert policy["credential_service"] == "wb_ads"
    assert policy["active_campaign_status"] == 9
    assert "wb_ads_audit_active" in policy["m0_tools"]
    assert policy["metric_class"] == "advertising_attribution_operational"
    assert "not actual business profit" in policy["profitability_boundary"]
    assert policy["archive_domain"] == "База данных/WB/<cabinet>/<year>/advertising"
    assert "dataset_coverage_registry.csv" in policy["archive_status"]
    assert "ads_campaign_daily" in policy["archive_v1_datasets"]
    assert "ads_campaign_roster_snapshots" in policy["archive_v1_datasets"]
    assert policy["semantic_metric_contract"] == "wb_ads_m0.v1"
    assert "cabinet-level" in policy["semantic_v1_scope"]
    assert "ads_product_daily" in policy["semantic_v1_scope"]
    assert "FULL_COVERAGE" in policy["historical_routing"]
    assert "marketplace_database_update_plan" in policy["refresh_scope_gate"]
    assert "7 closed days" in policy["refresh_policy"]
    assert policy["write_control_status"].startswith("not accepted in M0")
    assert "WRITE/DESTRUCTIVE" in policy["safety_override"]


def test_semantic_core_is_runtime_wired_for_finance_advertising_and_operational_metrics():
    semantic = SYSTEM_MAP["semantic_core"]
    assert semantic["status"] == "NATURAL_QUESTION_ROUTING_PARTIALLY_WIRED"
    assert "core/semantic_registry.yaml" in semantic["registry"]
    assert "semantic_registry_extensions.yaml" in semantic["registry"]
    assert semantic["intent_catalog"] == "core/semantic_intents.yaml"
    assert semantic["business_query_parser"].startswith("core/business_query_parser.py")
    assert semantic["resolver"] == "core/semantic_resolver.py"
    assert "semantic_execution.yaml" in semantic["execution_registry"]
    assert "semantic_archive.py" in semantic["archive_executor"]
    assert "semantic_advertising.py" in semantic["archive_executor"]
    assert "semantic_current_stock.py" in semantic["operational_executor"]
    assert semantic["runtime_entry"] == "marketplace_business_query"
    assert set(semantic["approved_operational_business_metrics"]) == {
        "ORDERS",
        "CURRENT_STOCK",
        "CURRENT_FBS_STOCK",
        "CURRENT_SELLING_PRICE",
        "OZON_BASE_PRICE",
        "OZON_OLD_PRICE",
        "OZON_MIN_PRICE",
    }
    assert "Seller Analytics" in semantic["current_stock_source"]
    assert "warehouse-remains" in semantic["current_stock_source"]
    assert "CURRENT_FBS_STOCK" in semantic["current_stock_source"]
    assert "read-only POST /api/v3/stocks/{warehouseId}" in semantic["current_stock_source"]
    assert "RUB" in semantic["current_price_source"]
    assert "never be divided by 100" in semantic["current_price_source"]
    assert set(semantic["current_archive_datasets"]) == {
        "wb_weekly_finance_main",
        "ads_campaign_daily",
        "ads_campaign_roster_snapshots",
    }
    assert set(semantic["approved_archive_executors"]) == {
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
    }
    assert "FULL_COVERAGE" in semantic["execution_gate"]
    assert "dataset_coverage_registry.csv" in semantic["execution_gate"]
    assert semantic["question_policy"]["precedence"] == "question overrides conflicting legacy metric"
    assert "operational business metric" in semantic["question_policy"]["current_state_precedence"]
    assert "preserves the original question" in semantic["runtime_integration"]
    assert "CURRENT_STOCK" in semantic["runtime_integration"]
    assert "advertising" in semantic["runtime_integration"].lower()


def test_current_stock_and_today_routing_boundaries_are_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "ordinary ORDERS questions including today" in rules
    assert "CURRENT_STOCK is CURRENT_OPERATIONAL_STOCK" in rules
    assert "CURRENT_FBS_STOCK is a separate CURRENT_SELLER_WAREHOUSE_STOCK metric" in rules
    assert "CURRENT_SELLING_PRICE for WB" in rules
    assert "divide_by_100 is forbidden" in rules
    assert "asynchronous warehouse-remains report fallback" in rules
    assert "historical requests must fail closed" in rules
    assert "generic current-state marker" in rules
    routing = SYSTEM_MAP["routing_policy"]["current_stock"]
    assert "current WB Seller Analytics stock snapshot" in routing
    assert "historical stock dates" in routing
    assert "today's snapshot" in routing


def test_advertising_semantic_guardrails_are_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "advertising_performance" in rules
    assert "campaign-roster" in rules
    assert "fullstats" in rules
    assert "nm_id/product" in rules
    assert "DRR/ROAS" in rules
    assert "current-day advertising" in rules
    routing = SYSTEM_MAP["routing_policy"]["advertising_live_vs_archive"]
    assert "archive-first" in routing
    assert "FULL_COVERAGE" in routing
    assert "product-level" in routing
    assert "fail-closed" in routing


def test_sales_and_returns_formula_is_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "saleDt" in rules
    assert "docTypeName" in rules
    assert "Продажа minus Возврат" in rules
    assert "sales/returns" in semantic["runtime_integration"]


def test_logistics_and_deductions_keep_components_separate():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "deliveryService and rebillLogisticCost separate" in rules
    assert "deduction and additionalPayment separate" in rules
    assert "never silently netted" in rules
    assert "logistics" in semantic["runtime_integration"]
    assert "deductions/adjustments" in semantic["runtime_integration"]


def test_wb_reward_and_acquiring_boundaries_are_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "vw and vwNds" in rules
    assert "commissionPercent/kvw/kvwBase" in rules
    assert "acquiringFee" in rules
    assert "preliminary" in rules.lower()
    assert "final monthly" in rules.lower()
    assert "monetary WB reward" in semantic["runtime_integration"]
    assert "preliminary weekly acquiring" in semantic["runtime_integration"]
    assert "commission-rate" in semantic["runtime_integration"].lower()


def test_historical_fulfillment_boundary_is_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "deliveryMethod" in rules
    assert "historical fulfillment" in rules
    assert "current configuration" in rules
    assert "historical fulfillment observations" in semantic["runtime_integration"]
    assert "current tariff/configuration questions fail closed" in semantic["runtime_integration"]


def test_historical_tariff_context_boundary_is_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "dlvPrc" in rules
    assert "fixTariffDateFrom" in rules
    assert "fixTariffDateTo" in rules
    assert "warehouseLogisticsCoeff" in rules
    assert "current live warehouse tariff" in rules
    assert "historical warehouse tariff context" in semantic["runtime_integration"]
    assert "live source" in SYSTEM_MAP["routing_policy"]["current_tariffs"]


def test_order_source_guardrail_is_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "Statistics Orders" in rules
    assert "operational/preliminary" in rules
    assert SYSTEM_MAP["routing_policy"]["complete_orders"].startswith(
        "do not substitute WB Statistics Orders"
    )


def test_database_refresh_routing_is_canonical():
    archive = SYSTEM_MAP["archive_policy"]
    routing = SYSTEM_MAP["routing_policy"]["update_database"]
    assert "marketplace_database_update" in routing
    assert "COMPLETE" in routing
    assert "marketplace_database_verify" in routing
    assert "stable key" in archive["update_behavior"]
    assert "date high-watermarks" in archive["post_refresh_verification"]


def test_server_instructions_contain_hard_architecture_boundaries():
    assert ARCHITECTURE_VERSION in SYSTEM_INSTRUCTIONS
    assert "dedicated Linux REMOTE server" in SYSTEM_INSTRUCTIONS
    assert "not Yandex Cloud" in SYSTEM_INSTRUCTIONS
    assert "https://mcp892081436.duckdns.org:13267/mcp" in SYSTEM_INSTRUCTIONS
    assert "OAuth/Keycloak" in SYSTEM_INSTRUCTIONS
    assert "127.0.0.1:18181" in SYSTEM_INSTRUCTIONS
    assert "marketplaces-yandex" in SYSTEM_INSTRUCTIONS
    assert "must not be used as a production fallback" in SYSTEM_INSTRUCTIONS
    assert "/opt/mcp/secrets/marketplaces/" in SYSTEM_INSTRUCTIONS
    assert "Do not treat Yandex Lockbox as the current production secret store" in SYSTEM_INSTRUCTIONS
    assert "127.0.0.1:6379" in SYSTEM_INSTRUCTIONS
    assert "Google Drive" in SYSTEM_INSTRUCTIONS
    assert "Apps Script" in SYSTEM_INSTRUCTIONS
    assert "DirectGoogleDriveArchiveStore" in SYSTEM_INSTRUCTIONS
    assert "Service Account" in SYSTEM_INSTRUCTIONS
    assert "ARCHIVE_SOURCE_UNAVAILABLE" in SYSTEM_INSTRUCTIONS
    assert "resumable session" in SYSTEM_INSTRUCTIONS
    assert "No Google OAuth refresh token" in SYSTEM_INSTRUCTIONS
    assert "effective-user OAuth token" in SYSTEM_INSTRUCTIONS
    assert "non-canonical staging file" in SYSTEM_INSTRUCTIONS
    assert "canonical large file must remain untouched" in SYSTEM_INSTRUCTIONS
    assert "Drive SHA256" in SYSTEM_INSTRUCTIONS
    assert "promote" in SYSTEM_INSTRUCTIONS
    assert "exponential backoff" in SYSTEM_INSTRUCTIONS
    assert "256 KiB" in SYSTEM_INSTRUCTIONS
    assert "Drive Range" in SYSTEM_INSTRUCTIONS
    assert "Yandex Object Storage" in SYSTEM_INSTRUCTIONS
    assert "legacy yandex-object-storage path" in SYSTEM_INSTRUCTIONS
    assert "active production durable backend" in SYSTEM_INSTRUCTIONS
    assert "read-only REMOTE backend audit" in SYSTEM_INSTRUCTIONS
    assert "source of truth" in SYSTEM_INSTRUCTIONS
    assert "marketplace_database_update" in SYSTEM_INSTRUCTIONS
    assert "marketplace_database_verify" in SYSTEM_INSTRUCTIONS
    assert "explicit marketplace, seller/cabinet scope and dataset family" in SYSTEM_INSTRUCTIONS
    assert "Never infer a missing value" in SYSTEM_INSTRUCTIONS
    assert "marketplace_database_update_plan" in SYSTEM_INSTRUCTIONS
    assert "most recent 7 closed days" in SYSTEM_INSTRUCTIONS
    assert "permanent finality" in SYSTEM_INSTRUCTIONS
    assert "date/high-watermark" in SYSTEM_INSTRUCTIONS
    assert "wb_ads" in SYSTEM_INSTRUCTIONS
    assert "actual business profit" in SYSTEM_INSTRUCTIONS
    assert "Semantic Core" in SYSTEM_INSTRUCTIONS
    assert "FULL_COVERAGE" in SYSTEM_INSTRUCTIONS
    assert "dataset_coverage_registry.csv" in SYSTEM_INSTRUCTIONS
    assert "wb_ads_m0.v1" in SYSTEM_INSTRUCTIONS
    assert "Product/nm_id" in SYSTEM_INSTRUCTIONS
    assert "advertising-attribution" in SYSTEM_INSTRUCTIONS
    assert "original wording" in SYSTEM_INSTRUCTIONS
    assert "operational/preliminary" in SYSTEM_INSTRUCTIONS
    assert "CURRENT_STOCK" in SYSTEM_INSTRUCTIONS
    assert "CURRENT_OPERATIONAL_STOCK" in SYSTEM_INSTRUCTIONS
    assert "warehouse-remains report fallback" in SYSTEM_INSTRUCTIONS
    assert "past-date stock question must fail closed" in SYSTEM_INSTRUCTIONS
    assert "saleDt" in SYSTEM_INSTRUCTIONS
    assert "Продажа minus Возврат" in SYSTEM_INSTRUCTIONS
    assert "deliveryService" in SYSTEM_INSTRUCTIONS
    assert "rebillLogisticCost" in SYSTEM_INSTRUCTIONS
    assert "deduction" in SYSTEM_INSTRUCTIONS
    assert "additionalPayment" in SYSTEM_INSTRUCTIONS
    assert "vw" in SYSTEM_INSTRUCTIONS
    assert "vwNds" in SYSTEM_INSTRUCTIONS
    assert "commissionPercent" in SYSTEM_INSTRUCTIONS
    assert "acquiringFee" in SYSTEM_INSTRUCTIONS
    assert "PRELIMINARY" in SYSTEM_INSTRUCTIONS
    assert "final monthly" in SYSTEM_INSTRUCTIONS
    assert "deliveryMethod" in SYSTEM_INSTRUCTIONS
    assert "officeName" in SYSTEM_INSTRUCTIONS
    assert "dlvPrc" in SYSTEM_INSTRUCTIONS
    assert "fixTariffDateFrom" in SYSTEM_INSTRUCTIONS
    assert "fixTariffDateTo" in SYSTEM_INSTRUCTIONS
    assert "warehouseLogisticsCoeff" in SYSTEM_INSTRUCTIONS
    assert "current live tariff" in SYSTEM_INSTRUCTIONS
    assert "fail closed" in SYSTEM_INSTRUCTIONS




def test_retired_yandex_runtime_cannot_become_canonical_by_regression():
    runtime = SYSTEM_MAP["runtime"]
    assert "cloud" not in runtime
    assert runtime["production"] == "dedicated Linux REMOTE server"
    assert "marketplaces-yandex" not in runtime["entry"]
    assert "Yandex API Gateway" not in runtime["entry"]
    assert "Yandex Serverless Container" not in runtime["entry"]
    assert "marketplaces-yandex" in runtime["retired_path"]
    assert "legacy" in runtime["retired_path"]
    assert SYSTEM_MAP["storage_policy"]["primary_archive_storage"] == "Google Drive"
    assert "legacy/migration-compatible" in SYSTEM_MAP["storage_policy"]["legacy_yandex_object_storage"]
    assert "Yandex Cloud only" not in SYSTEM_INSTRUCTIONS


def test_system_map_tool_is_registered_for_canonical_version():
    mcp = FastMCP("architecture-map-test")
    register_system_map_tool(mcp)
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert "marketplace_system_map" in tools
    assert SYSTEM_MAP["architecture_version"] == ARCHITECTURE_VERSION
    assert SYSTEM_MAP["status"] == "CANONICAL"


def test_human_and_agent_docs_reference_canonical_architecture_version():
    architecture = (ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert ARCHITECTURE_VERSION in architecture
    assert "core/system_map.py" in architecture
    assert "Canonical marketplace archive: **Google Drive**" in architecture
    assert "Google Apps Script" in architecture
    assert "DirectGoogleDriveArchiveStore" in architecture
    assert "ARCHIVE_SOURCE_UNAVAILABLE" in architecture
    assert "resumable" in architecture.lower()
    assert "Google Drive API" in architecture
    assert "non-canonical staging filename" in architecture
    assert "sha256Checksum" in architecture
    assert "promote_verified" in architecture
    assert "must not write directly into the existing canonical file" in architecture
    assert "marketplace_database_update" in architecture
    assert "marketplace_database_verify" in architecture
    assert "stable row key" in architecture
    assert "No Google OAuth refresh token" in SYSTEM_INSTRUCTIONS
    assert "Yandex Object Storage" in architecture
    assert "must be established from the actual REMOTE service environment/config" in architecture
    assert "retired as a production path" in architecture
    assert "Advertising Archive V1" in architecture
    assert "dataset_coverage_registry.csv" in architecture
    assert "advertising_performance" in architecture
    assert "wb_ads" in architecture
    assert "core/business_query_parser.py" in architecture
    assert "core/semantic_resolver.py" in architecture
    assert "core/semantic_execution.yaml" in architecture
    assert "core/semantic_archive.py" in architecture
    assert "core/semantic_registry_extensions.yaml" in architecture
    assert "core/semantic_advertising.py" in architecture
    assert "core/semantic_current_stock.py" in architecture
    assert "core/semantic_business_router.py" in architecture
    assert "original natural-language question" in architecture
    assert "CURRENT_OPERATIONAL_STOCK" in architecture
    assert "historical stock" in architecture.lower()
    assert "sale_and_return_operations" in architecture
    assert "deliveryService" in architecture
    assert "rebillLogisticCost" in architecture
    assert "deduction" in architecture
    assert "additionalPayment" in architecture
    assert "commission_and_wb_reward" in architecture
    assert "acquiring_and_payment_processing" in architecture
    assert "observed_fulfillment_method" in architecture
    assert "HISTORICAL_OBSERVED_FULFILLMENT" in architecture
    assert "warehouse_tariff_context" in architecture
    assert "HISTORICAL_APPLIED_WAREHOUSE_TARIFF_CONTEXT" in architecture
    assert "PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING" in architecture
    assert "PRELIMINARY_NOT_ALL_ORDERS" in architecture
    assert "core/system_map.py" in agents
    assert "Primary shared marketplace archive/storage is **Google Drive**" in agents
    assert "Google Apps Script" in agents
    assert "DirectGoogleDriveArchiveStore" in agents
    assert "ARCHIVE_SOURCE_UNAVAILABLE" in agents
    assert "resumable" in agents.lower()
    assert "refresh token" in agents
    assert "non-canonical staging filename" in agents
    assert "exact Drive size/SHA256" in agents
    assert "core/archive_yandex.py" in agents
    assert "legacy-capable implementation surfaces" in agents
    assert "Current production runtime is the dedicated Linux REMOTE server, not Yandex Cloud" in agents
    assert "https://mcp892081436.duckdns.org:13267/mcp" in agents
    assert "must not be restored or used as fallback" in agents
    assert "Archive durable backend after the REMOTE migration must be established from the actual REMOTE runtime" in agents
    assert "marketplace_database_update" in agents
    assert "marketplace_database_verify" in agents
    assert "stable row key" in agents
    assert "CURRENT_STOCK" in agents
    assert "historical stock" in agents.lower()
