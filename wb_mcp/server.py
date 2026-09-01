#!/usr/bin/env python3
"""wb_mcp — MCP server for the Wildberries Seller API.

Exposes the whole WB Seller API through schema-driven meta-tools (search /
describe / call / fetch_all) plus a few typed convenience tools for the most
common manager tasks. Multi-host aware. Credentials come from the environment.

Auth: WB uses ONE token, sent in the `Authorization` header as the raw value
(no "Bearer " prefix). The token is scoped per category — a token must include
the category of the host it calls.

Run:
    WB_API_TOKEN=... python -m wb_mcp.server
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from core.business_registry import resolve_business_cabinet
from core.client import MarketplaceClient, ServiceConfig
from core.entities import EntityIndex
from core.errors import make_error
from core.rate_limit import RateLimitUnavailable, build_rules
from core.registry import Catalog
from core.tools import register_cabinet_tools, register_generic_tools
from core.workflows import Workflows, register_workflow_tools

CATALOG_PATH = Path(__file__).with_name("endpoints.yaml")
WORKFLOWS_PATH = Path(__file__).with_name("workflows.yaml")


def _build_headers(creds: dict[str, str]) -> dict[str, str]:
    # Raw token, no "Bearer" prefix (per WB docs / community practice).
    return {"Authorization": creds.get("token", "")}


WB_CONFIG = ServiceConfig(
    name="wb",
    scheme="https",
    fields=["token"],
    env_map={"token": "WB_API_TOKEN"},
    build_headers=_build_headers,
    whoami=("wb_get_api_seller_info", ["name", "tradeMark"]),
    # WB is multi-host, but every host lives under wildberries.ru. Auth headers
    # (the raw seller token) may only ever be sent there.
    allowed_host_suffixes=[".wildberries.ru"],
)

mcp = FastMCP("wb_mcp")
entities = EntityIndex.load()
catalog = Catalog.from_yaml(CATALOG_PATH, entities=entities)
client = MarketplaceClient(WB_CONFIG)

# Register the 8 generic schema-driven tools (wb_search_methods, wb_call_method, ...)
register_generic_tools(
    mcp, svc="wb", client=client, catalog=catalog, entities=entities,
    key_help="seller.wildberries.ru → Settings → Access tokens (one token, "
             "select the categories you need).",
)
register_cabinet_tools(mcp, svc="wb", client=client, catalog=catalog)
register_workflow_tools(mcp, svc="wb", workflows=Workflows.from_yaml(WORKFLOWS_PATH))


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _calendar_date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date (YYYY-MM-DD)") from exc


def _orders_summary_cache_key(cabinet_key: str, day: date) -> str:
    identity = hashlib.sha256(cabinet_key.encode("utf-8")).hexdigest()
    return f"marketplace-summary:v1:wb:orders:{identity}:{day.isoformat()}:flag1"


def _orders_summary_ttl(day: date) -> int:
    # WB reports order changes roughly every 30 minutes. Yesterday and older are
    # closed calendar days, so a modest two-hour cache remains useful without
    # presenting a stale live-day total as current.
    return 7200 if day < date.today() else 1200


def _orders_summary_from_rows(rows: object, day: date, seller: str) -> dict:
    if not isinstance(rows, list):
        return make_error(
            "schema", "WB orders response is not an array.",
            operation_id="wb_stats_orders", retryable=False,
        )

    count = 0
    cancelled = 0
    total = Decimal("0")
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        try:
            row_day = _calendar_date(str(row.get("date", "")), "WB order date")
        except ValueError:
            skipped += 1
            continue
        if row_day != day:
            continue
        if bool(row.get("isCancel", False)):
            cancelled += 1
            continue
        try:
            amount = Decimal(str(row["finishedPrice"]))
        except (KeyError, InvalidOperation, ValueError):
            return make_error(
                "schema", "WB order row has no valid finishedPrice.",
                operation_id="wb_stats_orders", retryable=False,
            )
        count += 1
        total += amount

    quality = (
        "Orders are WB Statistics API rows created on the requested calendar day; "
        "isCancel=true rows are excluded; amount is buyer-paid finishedPrice."
    )
    if skipped:
        quality += f" Skipped {skipped} malformed row(s)."
    return {
        "ok": True,
        "seller": seller,
        "date_from": day.isoformat(),
        "date_to": day.isoformat(),
        "orders_count": count,
        "orders_amount": float(total),
        "currency": "RUB",
        "cancelled_orders_excluded": cancelled,
        "source": "wb_stats_orders",
        "quality": quality,
    }


# --------------------------------------------------------------------------
# Typed convenience tools — the everyday manager workflows, one call each.
# They delegate to the same client; nothing is duplicated.
# --------------------------------------------------------------------------
@mcp.tool(
    name="wb_get_sales",
    annotations={"title": "WB sales & returns", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_sales(date_from: str, flag: int = 0) -> str:
    """Get Wildberries sales and returns since a date (Statistics API, 1 req/min).

    Args:
        date_from: RFC3339 date/time in MSK, e.g. "2026-06-01" or "2026-06-01T00:00:00".
        flag: 0 = rows changed since date_from (incremental); 1 = rows dated on date_from.
    Returns JSON: {"ok": true, "status", "data": [ sale rows ]} or error envelope.
    Each row includes saleID, srid, nmId, totalPrice, forPay, lastChangeDate.
    """
    spec = catalog.get("wb_stats_sales")
    return _j(await client.call_spec(spec, query={"dateFrom": date_from, "flag": flag}))


@mcp.tool(
    name="wb_get_stocks",
    annotations={"title": "WB current stocks", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_stocks(date_from: str = "2020-01-01") -> str:
    """Get the current Wildberries stock snapshot (Statistics API, 1 req/min).

    Stocks have no history — this is a point-in-time snapshot. Use an early
    date_from to get the full current set.

    Args:
        date_from: RFC3339 date; default "2020-01-01" returns everything in stock now.
    Returns JSON: {"ok": true, "data": [ stock rows ]} with quantity, warehouseName, nmId.
    """
    spec = catalog.get("wb_stats_stocks")
    return _j(await client.call_spec(spec, query={"dateFrom": date_from}))


@mcp.tool(
    name="wb_get_new_orders",
    annotations={"title": "WB new FBS orders", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_new_orders() -> str:
    """Get new FBS assembly orders awaiting processing (Marketplace API).

    Returns JSON: {"ok": true, "data": {"orders": [...]}} — each order has id,
    rid, article, skus, createdAt, warehouseId.
    """
    spec = catalog.get("wb_fbs_orders_new")
    return _j(await client.call_spec(spec))


@mcp.tool(
    name="wb_get_orders_summary",
    annotations={"title": "WB orders summary for one calendar day", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_orders_summary(seller: str, date_from: str, date_to: str) -> str:
    """Return a compact WB order count and buyer-paid total for ONE calendar day.

    The tool makes one Statistics Orders request in WB's date mode (flag=1).
    ``orders_count`` counts non-cancelled Statistics API order rows created on
    that day. ``orders_amount`` sums their ``finishedPrice`` (buyer-paid price).
    Rows where WB sets ``isCancel=true`` are excluded from both values.
    """
    try:
        start = _calendar_date(date_from, "date_from")
        end = _calendar_date(date_to, "date_to")
    except ValueError as exc:
        return _j(make_error("invalid_params", str(exc), retryable=False))
    if start != end:
        return _j(make_error(
            "invalid_params",
            "wb_get_orders_summary currently accepts exactly one calendar day; "
            "use matching date_from and date_to.",
            retryable=False,
        ))

    business_cabinet = resolve_business_cabinet("wb", seller)
    credential_name = business_cabinet.cabinet if business_cabinet else seller
    creds, resolved_seller = client.config.store.resolve_named(
        "wb", client.config.fields, client.config.env_map, credential_name
    )
    if not resolved_seller or any(not creds.get(field) for field in client.config.fields):
        if business_cabinet:
            return _j(make_error(
                "seller_known_but_not_configured",
                f"Кабинет {business_cabinet.business_entity} известен, но доступ "
                "Wildberries для него ещё не настроен.",
                operation_id="wb_get_orders_summary",
                retryable=False,
                details={
                    "seller": seller,
                    "business_entity": business_cabinet.business_entity,
                    "cabinet": business_cabinet.cabinet,
                    "credentials_status": "not_configured",
                    "upstream_request_sent": False,
                },
            ))
        return _j(make_error(
            "invalid_params", f"WB cabinet {seller!r} was not found or is incomplete.",
            operation_id="wb_get_orders_summary", retryable=False,
        ))

    cabinet_key = client._creds_key(client.config, creds)
    cache_key = _orders_summary_cache_key(cabinet_key, start)
    try:
        cached = await client.rate_controller.cache_get(cache_key)
        if cached:
            result = json.loads(cached)
            if isinstance(result, dict):
                result["cache"] = "hit"
                return _j(result)
    except Exception:
        pass  # Cache is an optimisation only; rate limiting remains mandatory.

    spec = catalog.get("wb_stats_orders")
    assert spec is not None
    rules = build_rules(
        service="wb", cabinet_key=cabinet_key, host=spec.host,
        operation_id=spec.operation_id, scope=spec.scope,
        catalog_rate_limit=spec.rate_limit,
    )
    try:
        reserved, retry_after = await client.rate_controller.try_acquire(rules)
    except RateLimitUnavailable as exc:
        return _j(make_error(
            "rate_limit", f"Shared rate limiter is unavailable: {exc}",
            operation_id=spec.operation_id, retryable=True,
        ))
    if not reserved:
        return _j({
            "ok": False,
            "error": "rate_limit_busy",
            "message": "WB Statistics Orders is limited to one request per minute; no request was sent.",
            "retry_after_sec": max(1, math.ceil(retry_after)),
            "operation_id": spec.operation_id,
        })

    response = await client.call_spec(
        spec,
        query={"dateFrom": start.isoformat(), "flag": 1},
        creds_override=creds,
        rate_limit_preacquired=True,
        retry_on_transport=False,
        retry_on_429=False,
    )
    if not response.get("ok"):
        return _j(response)
    result = _orders_summary_from_rows(response.get("data"), start, resolved_seller)
    if result.get("ok"):
        result["cache"] = "miss"
        try:
            await client.rate_controller.cache_set(
                cache_key, json.dumps(result, ensure_ascii=False), _orders_summary_ttl(start)
            )
        except Exception:
            result["cache"] = "miss_not_stored"
    return _j(result)


@mcp.tool(
    name="wb_get_prices",
    annotations={"title": "WB prices & discounts", "readOnlyHint": True,
                 "openWorldHint": True},
)
async def wb_get_prices(limit: int = 1000, offset: int = 0,
                        filter_nm_id: Optional[int] = None) -> str:
    """Get current prices and discounts for products (Discounts-Prices API).

    Args:
        limit: page size (<=1000).
        offset: pagination offset.
        filter_nm_id: optional single nmID to filter by.
    Returns JSON: {"ok": true, "data": {"listGoods": [{nmID, sizes, discount, ...}]}}.
    """
    q = {"limit": min(limit, 1000), "offset": offset}
    if filter_nm_id is not None:
        q["filterNmID"] = filter_nm_id
    spec = catalog.get("wb_prices_list")
    return _j(await client.call_spec(spec, query=q))


@mcp.tool(
    name="wb_set_price",
    annotations={"title": "WB set price/discount", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False,
                 "openWorldHint": True},
)
async def wb_set_price(nm_id: int, price: int, discount: int = 0,
                       confirm_write: bool = False) -> str:
    """Set price and discount for ONE product (Discounts-Prices API). WRITE.

    Requires confirm_write=true (this changes your live price). A new price 3x
    below the old one lands the product in WB price quarantine.

    Args:
        nm_id: product nmID.
        price: new base price in rubles (integer).
        discount: discount percent (0-99).
        confirm_write: must be true to actually send the change.
    Returns JSON: {"ok": true, "data": {"id": uploadID}} — poll wb_prices_history_tasks.
    """
    from core.safety import check_gate
    gate = check_gate("write", confirm_write=confirm_write,
                      i_understand_this_modifies_data=True,
                      operation_id="wb_set_price", endpoint="/api/v2/upload/task")
    if gate:
        return _j(gate)
    spec = catalog.get("wb_prices_set")
    body = {"data": [{"nmID": nm_id, "price": price, "discount": discount}]}
    return _j(await client.call_spec(spec, json_body=body))


def main() -> None:
    """Console entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
