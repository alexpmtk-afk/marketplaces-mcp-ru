"""Internal MCP tools for maintaining canonical WB ORDERS history."""
from __future__ import annotations

import json
from typing import Any

from .business_registry import resolve_business_cabinet
from .errors import make_error
from .order_history import OrderHistoryStore, coverage_payload, sync_wb_orders_history


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def register_order_history_tools(
    combined: Any,
    modules: dict[str, Any],
    store: OrderHistoryStore | None,
) -> None:
    wb = modules["wb"]

    @combined.tool(
        name="wb_orders_history_sync",
        annotations={
            "title": "WB canonical ORDERS history sync",
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    async def wb_orders_history_sync(seller: str, bootstrap_days: int = 90) -> str:
        """Persist one safe page of canonical WB Statistics Orders into YDB.

        Intended for the server scheduler/maintenance path. It never modifies
        marketplace data; the write is only to the MCP's own durable history.
        """
        if store is None:
            return _j(make_error(
                "source_not_suitable",
                "WB ORDERS Historical Store is not configured on this MCP runtime.",
                operation_id="wb_orders_history_sync",
                retryable=False,
                details={"required_env": "MARKETPLACE_MCP_YDB_ENDPOINT"},
            ))
        return _j(await sync_wb_orders_history(
            wb, store, seller=seller, bootstrap_days=bootstrap_days,
        ))

    @combined.tool(
        name="wb_orders_history_status",
        annotations={
            "title": "WB canonical ORDERS history coverage",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def wb_orders_history_status(seller: str) -> str:
        """Return durable coverage without exposing credentials or storage keys."""
        entry = resolve_business_cabinet("wb", seller)
        cabinet = entry.cabinet if entry else seller.strip()
        if not cabinet:
            return _j(make_error(
                "invalid_params", "seller must be non-empty",
                operation_id="wb_orders_history_status", retryable=False,
            ))
        if store is None:
            return _j({
                "ok": True,
                "cabinet": cabinet,
                "configured": False,
                "storage": None,
                "status": "not_configured",
                "stored_rows": 0,
            })
        result = coverage_payload(store, cabinet)
        result["configured"] = True
        return _j(result)
