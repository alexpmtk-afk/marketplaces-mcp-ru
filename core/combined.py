"""Combined MCP server: Wildberries + Ozon + Ozon-Perf on one FastMCP.

One process, one stdio channel, every tool. This backs both the `.mcpb`
Claude Desktop bundle (`serve.py all`) and the `marketplaces-mcp-ru` console
script (`uvx marketplaces-mcp-ru`).

Tool names are already namespaced per service (``wb_*`` / ``ozon_*`` /
``ozon_perf_*``), so merging the three servers' tool sets can never collide.
Each service module builds its FastMCP as an import side effect; we copy the
already-registered tools onto a single parent via FastMCP's tool manager. That
internal surface is stable within the pinned ``mcp>=1.2,<2`` range.
"""
from __future__ import annotations

import asyncio
import importlib
import json
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .rate_limit import RateLimitUnavailable, verify_shared_redis

SERVICE_MODULES = ("wb_mcp.server", "ozon_mcp.server", "ozon_perf_mcp.server")


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _rate_status_tool(client: Any):
    async def status() -> str:
        backend = client.rate_controller.backend
        reachable = True
        error = None
        if backend == "redis":
            try:
                await asyncio.to_thread(verify_shared_redis)
            except RateLimitUnavailable as exc:
                reachable = False
                error = str(exc)
        return json.dumps({
            "ok": reachable,
            "backend": backend,
            "shared": backend == "redis",
            "reachable": reachable,
            "error": error,
        }, ensure_ascii=False)
    return status


def _register_finance_tools(combined: FastMCP, modules: dict[str, Any]) -> None:
    """Register high-signal read-only finance tools on the combined server.

    They intentionally delegate to the same schema catalog/client as generic
    call_method, so there is only one HTTP contract and one rate-limit path.
    """
    wb = modules["wb"]
    ozon = modules["ozon"]

    @combined.tool(
        name="wb_get_realization_report",
        annotations={"title": "WB realization report", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def wb_get_realization_report(
        date_from: str,
        date_to: str,
        limit: int = 100000,
        rrdid: int = 0,
    ) -> str:
        """Get WB realization report rows for an inclusive date range.

        Args:
            date_from/date_to: ISO dates YYYY-MM-DD.
            limit: page size, 1..100000.
            rrdid: pagination cursor; use 0 for the first page.
        """
        start = date.fromisoformat(date_from[:10])
        end = date.fromisoformat(date_to[:10])
        if start > end:
            raise ValueError("date_from must be <= date_to")
        limit = max(1, min(100000, int(limit)))
        spec = wb.catalog.get("wb_report_realization")
        if spec is None:
            raise RuntimeError("wb_report_realization contract is missing")
        return _j(await wb.client.call_spec(spec, query={
            "dateFrom": start.isoformat(), "dateTo": end.isoformat(),
            "limit": limit, "rrdid": int(rrdid),
        }))

    @combined.tool(
        name="ozon_get_accrual_types",
        annotations={"title": "Ozon finance accrual types", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def ozon_get_accrual_types() -> str:
        """List finance accrual types available to the current Ozon cabinet."""
        spec = ozon.catalog.get("ozon_finance_accrual_types")
        if spec is None:
            raise RuntimeError("ozon_finance_accrual_types contract is missing")
        return _j(await ozon.client.call_spec(spec, json_body={}))

    @combined.tool(
        name="ozon_get_accruals_by_day",
        annotations={"title": "Ozon finance accruals by day", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def ozon_get_accruals_by_day(day: str, last_id: str = "") -> str:
        """Get Ozon finance accruals for one day using the current cursor API."""
        value = date.fromisoformat(day[:10]).isoformat()
        spec = ozon.catalog.get("ozon_finance_accrual_by_day")
        if spec is None:
            raise RuntimeError("ozon_finance_accrual_by_day contract is missing")
        return _j(await ozon.client.call_spec(spec, json_body={
            "date": value, "last_id": last_id,
        }))

    @combined.tool(
        name="ozon_get_realization",
        annotations={"title": "Ozon monthly realization", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def ozon_get_realization(month: int, year: int) -> str:
        """Get the Ozon monthly realization report for month/year."""
        month = int(month)
        year = int(year)
        if not 1 <= month <= 12:
            raise ValueError("month must be in 1..12")
        if not 2000 <= year <= 2100:
            raise ValueError("year must be in 2000..2100")
        spec = ozon.catalog.get("ozon_finance_realization")
        if spec is None:
            raise RuntimeError("ozon_finance_realization contract is missing")
        return _j(await ozon.client.call_spec(spec, json_body={
            "month": month, "year": year,
        }))


def build(**fastmcp_kwargs: Any) -> FastMCP:
    """Return one FastMCP carrying every service's tools.

    Keyword arguments are passed only to the combined FastMCP constructor. This
    keeps the local stdio server unchanged while allowing the remote entry point
    to supply its HTTP host and port.
    """
    combined = FastMCP("marketplaces-mcp-ru", **fastmcp_kwargs)
    modules: dict[str, Any] = {}
    for mod_name in SERVICE_MODULES:
        mod = importlib.import_module(mod_name)
        combined._tool_manager._tools.update(mod.mcp._tool_manager._tools)
        svc = mod.client.config.name
        modules[svc] = mod
        combined.tool(
            name=f"{svc}_rate_limit_status",
            annotations={
                "title": f"{svc.upper()} shared rate-limit status",
                "readOnlyHint": True,
                "openWorldHint": False,
            },
        )(_rate_status_tool(mod.client))
    _register_finance_tools(combined, modules)
    return combined


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
