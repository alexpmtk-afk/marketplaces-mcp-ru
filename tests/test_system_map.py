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
    assert SYSTEM_MAP["status"] == "CANONICAL"
    assert SYSTEM_MAP["runtime"]["cloud"] == "Yandex Cloud only"
    storage = SYSTEM_MAP["storage_policy"]
    assert storage["primary_archive_storage"] == "Google Drive"
    assert storage["google_drive_root"] == "MCP архив базы данных"
    assert "Apps Script" in storage["google_drive_auth"]
    assert "Yandex Lockbox" in storage["google_drive_auth"]
    assert "job state" in storage["yandex_object_storage"]
    assert "backup" in storage["yandex_object_storage"]
    assert "not part of the runtime architecture" in storage["google_cloud"]
    assert "OAuth" in storage["google_cloud"]
    assert SYSTEM_MAP["archive_policy"]["canonical_source_of_truth"] == "Google Drive annual CSV plus reports registry"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["report_type"] == 1
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["logical_week"] == "Monday-Sunday"
    assert SYSTEM_MAP["archive_policy"]["wb_weekly_finance_main"]["row_deduplication"] == "(reportId, rrdId)"


def test_wb_advertising_m0_is_read_only_and_separate_from_profit():
    policy = SYSTEM_MAP["advertising_policy"]
    assert policy["current_scope"].startswith("Wildberries only")
    assert policy["phase"] == "WB Advertising M0 read-only"
    assert policy["credential_service"] == "wb_ads"
    assert policy["active_campaign_status"] == 9
    assert "wb_ads_audit_active" in policy["m0_tools"]
    assert "advertising_attribution_operational" == policy["metric_class"]
    assert "not actual business profit" in policy["profitability_boundary"]
    assert policy["archive_domain"] == "База данных/WB/<cabinet>/<year>/advertising"
    assert "not yet implemented" in policy["archive_status"]
    assert policy["write_control_status"].startswith("not accepted in M0")
    assert "WRITE/DESTRUCTIVE" in policy["safety_override"]


def test_semantic_core_is_partially_runtime_wired_for_natural_questions():
    semantic = SYSTEM_MAP["semantic_core"]
    assert semantic["status"] == "NATURAL_QUESTION_ROUTING_PARTIALLY_WIRED"
    assert semantic["registry"] == "core/semantic_registry.yaml"
    assert semantic["intent_catalog"] == "core/semantic_intents.yaml"
    assert semantic["resolver"] == "core/semantic_resolver.py"
    assert semantic["execution_registry"] == "core/semantic_execution.yaml"
    assert semantic["archive_executor"] == "core/semantic_archive.py"
    assert semantic["runtime_entry"] == "marketplace_business_query"
    assert semantic["current_archive_dataset"] == "wb_weekly_finance_main"
    assert set(semantic["approved_archive_executors"]) == {
        "penalties",
        "storage_charge",
        "acceptance_charge",
        "sale_and_return_operations",
        "logistics",
        "deductions_and_adjustments",
        "commission_and_wb_reward",
        "acquiring_and_payment_processing",
    }
    assert "FULL_COVERAGE" in semantic["execution_gate"]
    assert semantic["question_policy"]["precedence"] == "question overrides conflicting legacy metric"
    assert "accepts the original question" in semantic["runtime_integration"]


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
    assert "Commission-rate questions" in semantic["runtime_integration"]


def test_order_source_guardrail_is_canonical():
    semantic = SYSTEM_MAP["semantic_core"]
    rules = "\n".join(semantic["rules"])
    assert "Statistics Orders" in rules
    assert "operational/preliminary" in rules
    assert SYSTEM_MAP["routing_policy"]["complete_orders"].startswith(
        "do not substitute WB Statistics Orders"
    )


def test_server_instructions_contain_hard_architecture_boundaries():
    assert ARCHITECTURE_VERSION in SYSTEM_INSTRUCTIONS
    assert "Yandex Cloud" in SYSTEM_INSTRUCTIONS
    assert "Google Drive" in SYSTEM_INSTRUCTIONS
    assert "Apps Script" in SYSTEM_INSTRUCTIONS
    assert "Yandex Object Storage" in SYSTEM_INSTRUCTIONS
    assert "Yandex Lockbox" in SYSTEM_INSTRUCTIONS
    assert "source of truth" in SYSTEM_INSTRUCTIONS
    assert "Google Cloud is not part" in SYSTEM_INSTRUCTIONS
    assert "WB Advertising M0" in SYSTEM_INSTRUCTIONS
    assert "wb_ads" in SYSTEM_INSTRUCTIONS
    assert "actual business profit" in SYSTEM_INSTRUCTIONS
    assert "Semantic Core" in SYSTEM_INSTRUCTIONS
    assert "FULL_COVERAGE" in SYSTEM_INSTRUCTIONS
    assert "original wording" in SYSTEM_INSTRUCTIONS
    assert "operational/preliminary" in SYSTEM_INSTRUCTIONS
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
    assert "fail closed" in SYSTEM_INSTRUCTIONS


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
    assert "Yandex Object Storage" in architecture
    assert "WB Advertising M0" in architecture
    assert "wb_ads" in architecture
    assert "core/semantic_resolver.py" in architecture
    assert "core/semantic_execution.yaml" in architecture
    assert "core/semantic_archive.py" in architecture
    assert "original natural-language question" in architecture
    assert "sale_and_return_operations" in architecture
    assert "deliveryService" in architecture
    assert "rebillLogisticCost" in architecture
    assert "deduction" in architecture
    assert "additionalPayment" in architecture
    assert "commission_and_wb_reward" in architecture
    assert "acquiring_and_payment_processing" in architecture
    assert "PRELIMINARY_WEEKLY_PAYMENT_ACCEPTANCE_WITHHOLDING" in architecture
    assert "PRELIMINARY_NOT_ALL_ORDERS" in architecture
    assert "core/system_map.py" in agents
    assert "Primary shared marketplace archive/storage is **Google Drive**" in agents
    assert "Google Apps Script" in agents
    assert "Yandex Object Storage" in agents
