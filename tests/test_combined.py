"""Combined server mounts every seller API service plus approved cross-service tools."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import combined  # noqa: E402


def _tool_names(mcp) -> set[str]:
    return {t.name for t in asyncio.run(mcp.list_tools())}


def test_combined_contains_exact_services_plus_approved_combined_tools():
    import importlib

    per_service: set[str] = set()
    for mod_name in combined.SERVICE_MODULES:
        mod = importlib.import_module(mod_name)
        names = _tool_names(mod.mcp)
        assert names, f"{mod_name} exposed no tools"
        assert not (per_service & names), f"tool name collision from {mod_name}"
        per_service |= names

    approved_combined = {
        "wb_rate_limit_status",
        "ozon_rate_limit_status",
        "ozon_perf_rate_limit_status",
        "wb_get_realization_report",
        "ozon_get_accrual_types",
        "ozon_get_accruals_by_day",
        "ozon_get_realization",
        "card_monitor_status",
        "card_monitor_get_latest",
        "card_monitor_get_history",
        "card_monitor_compare_prices",
    }
    got = _tool_names(combined.build())
    assert got == per_service | approved_combined, (
        "combined server is not the service union plus approved combined tools "
        f"(missing: {(per_service | approved_combined) - got}, "
        f"extra: {got - (per_service | approved_combined)})"
    )


def test_rate_status_shows_safe_wait_time_without_queue_identity():
    class Controller:
        backend = "redis"

    class Client:
        rate_controller = Controller()

        async def rate_limit_status(self):
            return {
                "ok": True,
                "backend": "redis",
                "configured_global_rps": 50.0,
                "active_queues": [{
                    "queue": "catalog_group",
                    "queue_id": "opaque-hash",
                    "wait_seconds": 58.2,
                }],
            }

    result = json.loads(asyncio.run(combined._rate_status_tool(Client())()))
    assert result == {
        "ok": True,
        "backend": "redis",
        "shared": True,
        "reachable": True,
        "configured_global_rps": 50.0,
        "active_queues": [{"queue": "catalog_group", "wait_seconds": 58.2}],
        "error": None,
    }
