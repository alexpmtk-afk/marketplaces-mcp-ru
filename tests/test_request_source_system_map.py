from __future__ import annotations

# Importing the runtime extension is what the combined server does before it
# imports SYSTEM_INSTRUCTIONS from core.system_map.
from core import request_source_system_map as _request_source_system_map  # noqa: F401
from core.system_map import SYSTEM_INSTRUCTIONS, SYSTEM_MAP


def test_top_level_source_router_is_exposed_in_runtime_architecture_map():
    router = SYSTEM_MAP["request_source_router"]
    assert router["status"] == "EXECUTION_PLAN_V2"
    assert router["runtime_entry"] == "marketplace_query_plan"
    assert set(router["source_families"]) == {
        "CANONICAL_ARCHIVE",
        "LIVE_CABINET_API",
        "PUBLIC_MARKETPLACE_SOURCE",
        "SYSTEM_INTERNAL",
        "HYBRID",
        "UNAVAILABLE",
    }
    execution = router["execution_plan"]
    assert execution["version"] == "marketplace_execution_plan.v2"
    assert set(execution["statuses"]) == {
        "READY",
        "READY_WITH_GATES",
        "NEEDS_CONTEXT",
        "BLOCKED",
    }
    assert "explicit Semantic contract" in execution["join_policy"]
    assert router["availability_facts"]["wb_orders_historical_archive"] is False
    assert router["availability_facts"]["wb_stock_historical_archive"] is False
    assert "marketplace_query_plan" in SYSTEM_MAP["routing_policy"]["top_level_request"]


def test_clarification_gate_is_top_level_and_fail_closed():
    router = SYSTEM_MAP["request_source_router"]
    clarification = router["clarification_gate"]
    assert clarification["position"] == "after marketplace_query_plan and before any data executor"
    assert clarification["interaction_state"] == "CLARIFICATION_REQUIRED"
    assert any("NEEDS_CONTEXT" in item for item in clarification["triggers"])
    assert any("AMBIGUOUS" in item for item in clarification["triggers"])
    assert any("UNKNOWN" in item for item in clarification["triggers"])
    assert "must not execute" in clarification["assistant_response_policy"]
    assert "call marketplace_query_plan again" in clarification["resume_policy"]
    assert "never resolve uncertainty by silent inference" in clarification["loop_policy"]


def test_clarification_gate_distinguishes_unknown_from_real_source_gap():
    clarification = SYSTEM_MAP["request_source_router"]["clarification_gate"]
    assert "source gap" in clarification["known_source_gap_behavior"]
    assert "do not ask for clarification" in clarification["recognized_multi_source_behavior"]


def test_execution_controller_is_between_clarification_and_data_executors():
    controller = SYSTEM_MAP["request_source_router"]["execution_controller"]
    assert controller["status"] == "CONTRACT_DISPATCH_V1"
    assert controller["runtime_entry"] == "marketplace_execution_control"
    assert controller["controller_version"] == "marketplace_execution_controller.v1"
    assert controller["leg_contract_version"] == "marketplace_leg_execution.v1"
    assert "after Clarification Gate" in controller["position"]
    assert "must never be copied unchanged" in controller["compound_question_policy"]
    assert "exact arguments" in controller["dispatch_policy"]
    assert "no required leg is dispatchable" in controller["all_or_nothing_policy"]
    assert "prohibit arithmetic" in controller["join_policy"]


def test_server_instructions_require_execution_planning_before_semantic_core():
    assert "marketplace_query_plan as the first server-side planning step" in SYSTEM_INSTRUCTIONS
    assert "marketplace_execution_plan.v2" in SYSTEM_INSTRUCTIONS
    assert "Code/module presence never proves" in SYSTEM_INSTRUCTIONS
    assert "no verified populated WB historical orders archive" in SYSTEM_INSTRUCTIONS
    assert "no historical stock archive" in SYSTEM_INSTRUCTIONS
    assert "fail closed" in SYSTEM_INSTRUCTIONS
    assert "cross-source arithmetic" in SYSTEM_INSTRUCTIONS
    assert "Clarification Gate" in SYSTEM_INSTRUCTIONS
    assert "Never guess the closest metric" in SYSTEM_INSTRUCTIONS
    assert "must not execute any data leg" in SYSTEM_INSTRUCTIONS
    assert "Do not resume the old ambiguous plan directly" in SYSTEM_INSTRUCTIONS
    assert "marketplace_execution_control before every provider/archive/card executor" in SYSTEM_INSTRUCTIONS
    assert "exact executor_arguments" in SYSTEM_INSTRUCTIONS
    assert "Never pass the original compound multi-source user question unchanged" in SYSTEM_INSTRUCTIONS
    assert "dispatch_contracts must be empty" in SYSTEM_INSTRUCTIONS
