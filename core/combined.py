"""Combined MCP server: Wildberries + Ozon + Ozon-Perf + public card monitor."""
from __future__ import annotations

import importlib
import json
from datetime import date
from typing import Any

from mcp.server.fastmcp import FastMCP

from .business_registry import resolve_business_cabinet
from .business_router import register_business_query_tool
from .card_monitor import register_tools as register_card_monitor_tools
from .order_history_tools import register_order_history_tools
from .tools import resolve_named_cabinet
from .ydb_order_history import build_order_history_store_from_env

SERVICE_MODULES = ("wb_mcp.server", "ozon_mcp.server", "ozon_perf_mcp.server")


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _rate_status_tool(client: Any):
    async def status() -> str:
        """Expose queue health and wait time, never credentials or queue keys."""
        state = await client.rate_limit_status()
        backend = state.get("backend", client.rate_controller.backend)
        if not state.get("ok"):
            return json.dumps({
                "ok": False,
                "backend": backend,
                "shared": backend == "redis",
                "reachable": False,
                "active_queues": [],
                "error": state.get("message", "Rate-limit status is unavailable."),
            }, ensure_ascii=False)

        queues = [{
            "queue": item.get("queue", "other"),
            "wait_seconds": item.get("wait_seconds", 0.0),
        } for item in state.get("active_queues", [])]
        return json.dumps({
            "ok": True,
            "backend": backend,
            "shared": backend == "redis",
            "reachable": True,
            "configured_global_rps": state.get("configured_global_rps"),
            "active_queues": queues,
            "error": None,
        }, ensure_ascii=False)
    return status


def _resolve_wb_finance_creds(wb: Any, seller: str) -> tuple[dict[str, str] | None, dict | None]:
    """Resolve a WB business alias/canonical seller to one explicit cabinet."""
    business_cabinet = resolve_business_cabinet("wb", seller)
    credential_name = business_cabinet.cabinet if business_cabinet else seller
    creds, error = resolve_named_cabinet(wb.client, credential_name)
    if error and business_cabinet:
        error = {
            **error,
            "seller": seller,
            "business_entity": business_cabinet.business_entity,
            "cabinet": business_cabinet.cabinet,
        }
    return creds, error


def _register_finance_tools(combined: FastMCP, modules: dict[str, Any]) -> None:
    """Register high-signal read-only finance tools on the combined server."""
    wb = modules["wb"]
    ozon = modules["ozon"]

    @combined.tool(
        name="wb_list_realization_reports",
        annotations={"title": "WB realization reports list", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def wb_list_realization_reports(
        seller: str,
        date_from: str,
        date_to: str,
        period: str = "weekly",
        limit: int = 1000,
        offset: int = 0,
    ) -> str:
        """List current WB realization reports for one explicitly named cabinet.

        Use this first for archive discovery. ``period`` is ``weekly`` or ``daily``.
        The returned rows include ``reportId``; use that ID with
        ``wb_get_realization_report_by_id``. The current WB method requires a
        Personal or Service token with the Finance category.
        """
        start = date.fromisoformat(date_from[:10])
        end = date.fromisoformat(date_to[:10])
        if start > end:
            raise ValueError("date_from must be <= date_to")
        period = str(period).strip().lower()
        if period not in {"weekly", "daily"}:
            raise ValueError("period must be 'weekly' or 'daily'")
        limit = max(1, min(1000, int(limit)))
        offset = max(0, int(offset))
        creds, error = _resolve_wb_finance_creds(wb, seller)
        if error:
            return _j(error)
        spec = wb.catalog.get("wb_finance_sales_reports_list")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_list contract is missing")
        return _j(await wb.client.call_spec(
            spec,
            json_body={
                "dateFrom": start.isoformat(),
                "dateTo": end.isoformat(),
                "period": period,
                "limit": limit,
                "offset": offset,
            },
            creds_override=creds,
        ))

    @combined.tool(
        name="wb_get_realization_report_by_id",
        annotations={"title": "WB realization report details by ID", "readOnlyHint": True,
                     "openWorldHint": True},
    )
    async def wb_get_realization_report_by_id(
        seller: str,
        report_id: int,
        limit: int = 100000,
        rrd_id: int = 0,
    ) -> str:
        """Get one page of full WB realization-report details by ``reportId``.

        ``fields`` is deliberately omitted so WB returns every available column.
        Start with ``rrd_id=0``. If rows are returned, the archive worker should
        continue from the last row's ``rrdId`` until WB returns HTTP 204. The
        provider limit is one request per minute per seller account; this tool
        therefore remains fail-fast when the shared limiter says the next slot is
        not yet available instead of sleeping inside an interactive MCP call.
        """
        report_id = int(report_id)
        if report_id <= 0:
            raise ValueError("report_id must be a positive integer")
        limit = max(1, min(100000, int(limit)))
        rrd_id = max(0, int(rrd_id))
        creds, error = _resolve_wb_finance_creds(wb, seller)
        if error:
            return _j(error)
        spec = wb.catalog.get("wb_finance_sales_reports_detailed_by_id")
        if spec is None:
            raise RuntimeError("wb_finance_sales_reports_detailed_by_id contract is missing")
        return _j(await wb.client.call_spec(
            spec,
            path_values={"reportId": report_id},
            json_body={"limit": limit, "rrdId": rrd_id},
            creds_override=creds,
        ))

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
    """Return one FastMCP carrying seller APIs and server-native business routing."""
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

    order_history_store = build_order_history_store_from_env()
    modules["_order_history_store"] = order_history_store

    _register_finance_tools(combined, modules)
    register_business_query_tool(combined, modules)
    register_order_history_tools(combined, modules, order_history_store)
    register_card_monitor_tools(combined)
    return combined


def main() -> None:
    build().run()


if __name__ == "__main__":
    main()
