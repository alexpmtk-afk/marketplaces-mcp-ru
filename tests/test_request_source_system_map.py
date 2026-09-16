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


def test_server_instructions_require_execution_planning_before_semantic_core():
    assert "marketplace_query_plan as the first server-side planning step" in SYSTEM_INSTRUCTIONS
    assert "marketplace_execution_plan.v2" in SYSTEM_INSTRUCTIONS
    assert "Code/module presence never proves" in SYSTEM_INSTRUCTIONS
    assert "no verified populated WB historical orders archive" in SYSTEM_INSTRUCTIONS
    assert "no historical stock archive" in SYSTEM_INSTRUCTIONS
    assert "fail closed" in SYSTEM_INSTRUCTIONS
    assert "cross-source arithmetic" in SYSTEM_INSTRUCTIONS
