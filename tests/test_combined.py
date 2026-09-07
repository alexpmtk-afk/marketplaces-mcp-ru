"""Combined server (.mcpb / `uvx marketplaces-mcp-ru`) mounts every service.

The combined FastMCP is built by copying each service's already-registered
tools via FastMCP's internal tool manager (``_tool_manager._tools``). That
surface is private, so this test locks it: if an ``mcp`` upgrade moves it, the
combined tool count drops and this fails loudly instead of shipping an empty
Claude Desktop bundle. Tool names are namespaced per service. The remote-capable
combined server additionally exposes a small approved set of cross-service
infrastructure diagnostics and high-signal finance convenience tools.
"""
from __future__ import annotations

import asyncio
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
    }
    got = _tool_names(combined.build())
    assert got == per_service | approved_combined, (
        "combined server is not the service union plus approved combined tools "
        f"(missing: {(per_service | approved_combined) - got}, "
        f"extra: {got - (per_service | approved_combined)})"
    )
