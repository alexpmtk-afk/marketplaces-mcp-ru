from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from core import system_map
from core.semantic_core import (
    SEMANTIC_CORE_STATUS,
    SEMANTIC_CORE_VERSION,
    build_semantic_core_snapshot,
    register_semantic_core_tool,
    semantic_core_view,
)


def test_unified_semantic_core_composes_every_business_logic_layer() -> None:
    brain = build_semantic_core_snapshot()

    assert brain["version"] == SEMANTIC_CORE_VERSION
    assert brain["status"] == SEMANTIC_CORE_STATUS
    assert brain["validated"] is True
    assert brain["canonical_flow"] == [
        "UNDERSTAND",
        "RESOLVE",
        "PLAN_SOURCE",
        "CLARIFY",
        "DISPATCH",
        "EXECUTE",
        "JOIN",
        "CALCULATE_IF_REGISTERED",
        "ANSWER_WITH_PROVENANCE",
    ]

    assert brain["data_semantics"]["policy"]["fail_closed"] is True
    assert brain["understanding"]["intents"]["policy"]["fail_closed_on_unknown"] is True
    assert brain["execution"]["archive_execution_registry"]["policy"]["fail_closed"] is True
    assert brain["execution"]["archive_execution_registry"]["policy"]["require_full_coverage"] is True
    assert brain["planning"]["source_family_before_provider_field"] is True
    assert brain["planning"]["silent_substitution_forbidden"] is True
    assert brain["execution"]["dispatch"]["required_legs_all_or_nothing"] is True
    assert brain["join_control"]["arithmetic_permission_is_separate"] is True


def test_brain_uses_live_join_and_calculation_registries() -> None:
    brain = build_semantic_core_snapshot()

    assert brain["join_control"]["registered_contract_ids"] == [
        "wb_sales_vs_advertising_side_by_side.v1"
    ]
    join_contract = brain["join_control"]["contracts"][0]
    assert join_contract["comparison_mode"] == "SIDE_BY_SIDE_ONLY"
    assert join_contract["arithmetic_allowed"] is False
    assert join_contract["semantic_targets"] == [
        {"type": "CAPABILITY", "id": "advertising_performance"},
        {"type": "CAPABILITY", "id": "sale_and_return_operations"},
    ]

    calculation = brain["calculation_control"]
    assert calculation["registry_version"] == "marketplace_calculation_registry.v1"
    assert calculation["contract_version"] == "marketplace_calculation_contract.v1"
    assert calculation["registered_calculation_ids"] == []
    assert calculation["registered_calculation_count"] == 0
    assert calculation["arithmetic_enabled"] is False
    assert calculation["policy"]["currency_alignment"] == "EXPLICIT_ONLY"
    assert calculation["policy"]["implicit_currency_conversion_forbidden"] is True


def test_cross_layer_executor_targets_are_registered_capabilities() -> None:
    brain = build_semantic_core_snapshot()
    capabilities = set(brain["data_semantics"]["capabilities"])
    executors = set(brain["execution"]["archive_execution_registry"]["executors"])

    assert executors
    assert executors <= capabilities


def test_brain_exposes_known_gaps_instead_of_hiding_them() -> None:
    brain = build_semantic_core_snapshot()

    assert brain["gaps"]["registry"] or brain["gaps"]["inline"]
    assert brain["planning"]["availability_facts"]["wb_orders_historical_archive"] is False
    assert brain["planning"]["availability_facts"]["wb_stock_historical_archive"] is False
    assert brain["summary"]["counts"]["known_source_gaps"] > 0


def test_semantic_core_views_are_composed_not_parallel_registries() -> None:
    summary = semantic_core_view("summary")
    planning = semantic_core_view("planning")
    registry = semantic_core_view("registry")
    all_layers = semantic_core_view("all")

    assert summary["status"] == "CANONICAL_BRAIN"
    assert planning == all_layers["planning"]
    assert registry == all_layers["data_semantics"]
    assert summary["component_versions"] == all_layers["component_versions"]
    assert summary["component_versions"]["semantic_core"] == SEMANTIC_CORE_VERSION
    assert summary["counts"]["calculation_contracts"] == 0


def test_semantic_core_marks_system_map_brain_authority_without_hiding_source_gaps() -> None:
    semantic_map = system_map.SYSTEM_MAP["semantic_core"]

    assert semantic_map["status"] == "NATURAL_QUESTION_ROUTING_PARTIALLY_WIRED"
    assert semantic_map["brain_status"] == "CANONICAL_BRAIN"
    assert semantic_map["brain_version"] == SEMANTIC_CORE_VERSION
    assert semantic_map["brain_runtime_entry"] == "marketplace_semantic_core"
    assert "marketplace_semantic_core is the canonical composed business brain" in system_map.SYSTEM_INSTRUCTIONS
    assert "Physical source/executor coverage may still be incomplete" in system_map.SYSTEM_INSTRUCTIONS


def test_semantic_core_tool_is_registered_read_only() -> None:
    mcp = FastMCP("semantic-core-test")
    register_semantic_core_tool(mcp)

    tool = mcp._tool_manager._tools["marketplace_semantic_core"]
    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.openWorldHint is False
