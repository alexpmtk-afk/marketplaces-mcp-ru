"""Runtime architecture-map extension for the top-level request source router.

The infra repository layers its own later architecture version over the app
snapshot. This extension therefore adds routing policy and server instructions
without independently renumbering the base app map.
"""
from __future__ import annotations

from . import system_map as _system_map

_ROUTER_MAP = {
    "status": "EXECUTION_PLAN_V2",
    "runtime_entry": "marketplace_query_plan",
    "position": "first server-side planning layer before metric-specific Semantic Core",
    "source_families": [
        "CANONICAL_ARCHIVE",
        "LIVE_CABINET_API",
        "PUBLIC_MARKETPLACE_SOURCE",
        "SYSTEM_INTERNAL",
        "HYBRID",
        "UNAVAILABLE",
    ],
    "execution_plan": {
        "version": "marketplace_execution_plan.v2",
        "statuses": ["READY", "READY_WITH_GATES", "NEEDS_CONTEXT", "BLOCKED"],
        "required_leg_policy": "all required legs must be available; otherwise final answer fails closed",
        "join_policy": "no cross-source arithmetic without an explicit Semantic contract",
        "provenance_policy": "every leg keeps its own source family, source status, executor and coverage gate",
        "parallel_policy": "independent multi-source legs may execute in parallel only after each leg is validated",
    },
    "availability_facts": {
        "wb_orders_historical_archive": False,
        "wb_stock_historical_archive": False,
    },
    "rules": [
        "source family is selected before report/API fields",
        "code presence does not prove historical data exists",
        "missing archive history is never replaced with a current snapshot",
        "recent WB orders may use the operational cabinet API only inside its supported retention window",
        "historical WB stock is unavailable until a real historical source is populated and approved",
        "public marketplace facts are not substituted with private seller-cabinet values",
        "multi-domain questions produce separate source legs before any join",
        "every execution leg records executor, gate, missing context and forbidden substitutes",
        "a missing required leg blocks the final joined answer instead of allowing silent partial truth",
    ],
}

_system_map.SYSTEM_MAP["request_source_router"] = _ROUTER_MAP
_system_map.SYSTEM_MAP["routing_policy"]["top_level_request"] = (
    "call marketplace_query_plan first; create an execution plan with separate source legs, gates and blockers; "
    "then choose archive, live cabinet/API, public marketplace/card, system-internal, hybrid, or unavailable "
    "before metric-specific Semantic Core"
)

_EXTRA_INSTRUCTIONS = """
For every natural marketplace request, use marketplace_query_plan as the first server-side planning step before choosing archive, seller-cabinet/API, public-card/site, or other data tools.
The top-level planner chooses a source family before lower Semantic Core chooses a metric, report, field, or endpoint. Code/module presence never proves that historical data is actually populated.
The planner returns marketplace_execution_plan.v2: every required source leg has its own source family, source status, downstream executor, execution status, coverage gate, required context and forbidden substitutes.
There is currently no verified populated WB historical orders archive and no historical stock archive. Recent WB orders may use the operational cabinet API only within its supported retention window; older order history must fail closed. Historical stock must fail closed and must never receive today's snapshot.
Public marketplace/card facts are a separate source family from private seller-cabinet facts. If an on-demand public source is not connected, report that source gap instead of substituting cabinet or archive data.
Questions that require several domains must produce separate source legs and validate each leg before joining results. All required legs are mandatory by default; if one required leg is unavailable, the final joined answer must fail closed rather than silently returning a partial answer.
Never perform cross-source arithmetic or derived business calculations merely because two source legs exist. Arithmetic joins require an explicit approved Semantic contract, and provenance must be preserved for every leg.
"""
if "marketplace_query_plan as the first server-side planning step" not in _system_map.SYSTEM_INSTRUCTIONS:
    _system_map.SYSTEM_INSTRUCTIONS += _EXTRA_INSTRUCTIONS
