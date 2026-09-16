"""Runtime architecture-map extension for the top-level request source router.

The infra repository layers its own later architecture version over the app
snapshot. This extension therefore adds routing policy and server instructions
without independently renumbering the base app map.
"""
from __future__ import annotations

from . import system_map as _system_map

_ROUTER_MAP = {
    "status": "FOUNDATION_V1",
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
    ],
}

_system_map.SYSTEM_MAP["request_source_router"] = _ROUTER_MAP
_system_map.SYSTEM_MAP["routing_policy"]["top_level_request"] = (
    "call marketplace_query_plan first; choose archive, live cabinet/API, public marketplace/card, "
    "system-internal, hybrid, or unavailable before metric-specific Semantic Core"
)

_EXTRA_INSTRUCTIONS = """
For every natural marketplace request, use marketplace_query_plan as the first server-side planning step before choosing archive, seller-cabinet/API, public-card/site, or other data tools.
The top-level planner chooses a source family before lower Semantic Core chooses a metric, report, field, or endpoint. Code/module presence never proves that historical data is actually populated.
There is currently no verified populated WB historical orders archive and no historical stock archive. Recent WB orders may use the operational cabinet API only within its supported retention window; older order history must fail closed. Historical stock must fail closed and must never receive today's snapshot.
Public marketplace/card facts are a separate source family from private seller-cabinet facts. If an on-demand public source is not connected, report that source gap instead of substituting cabinet or archive data.
Questions that require several domains must produce separate source legs and validate each leg before joining results.
"""
if "marketplace_query_plan as the first server-side planning step" not in _system_map.SYSTEM_INSTRUCTIONS:
    _system_map.SYSTEM_INSTRUCTIONS += _EXTRA_INSTRUCTIONS
