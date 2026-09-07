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

import asyncio
import base64
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


def _wb_active_token_type() -> str:
    """Return a non-secret WB token type derived from the documented JWT `acc` claim."""
    creds, _source = client.config.resolve_creds()
    token = str(creds.get("token", "") or "")
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return {1: "base", 2: "test", 3: "personal", 4: "service"}.get(
            int(claims.get("acc", 0) or 0), "unknown"
        )
    except (IndexError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return "unknown"


def _wb_stocks_needs_report_fallback(result: dict) -> bool:
    if int(result.get("code", 0) or 0) != 403:
        return False
    text = " ".join(str(result.get(key, "")) for key in ("message", "details", "error"))
    return "token does not satisfy additional requirements" in text.lower()


def _wb_report_task_id(result: dict) -> Optional[str]:
    body = result.get("data")
    if not isinstance(body, dict):
        return None
    nested = body.get("data")
    if isinstance(nested, dict) and nested.get("taskId"):
        return str(nested["taskId"])
    if body.get("taskId"):
        return str(body["taskId"])
    return None


def _wb_flatten_remains(rows: object, nm_ids: Optional[list[int]]) -> list[dict]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("WB warehouse-remains report is not an array")
    wanted = set(nm_ids or [])
    out: list[dict] = []
    for product in rows:
        if not isinstance(product, dict):
            continue
        nm_id = product.get("nmId")
        if wanted and nm_id not in wanted:
            continue
        common = {
            key: product.get(key)
            for key in ("nmId", "vendorCode", "barcode", "techSize")
            if product.get(key) is not None
        }
        warehouses = product.get("warehouses")
        if not isinstance(warehouses, list):
            continue
        for warehouse in warehouses:
            if not isinstance(warehouse, dict):
                continue
            row = dict(common)
            if warehouse.get("warehouseName") is not None:
                row["warehouseName"] = warehouse.get("warehouseName")
            row["quantity"] = warehouse.get("quantity", 0)
            out.append(row)
    return out


async def _wb_report_request(
    path: str, *, operation_id: str, rate_scope: str,
) -> dict:
    """Run one safe WB report GET while absorbing only tiny local transport pacing.

    The report workflow performs several sequential GETs. Each endpoint has its
    own 15-minute Base-token quota, but all requests also share the sub-second WB
    transport pacing bucket. A following step can therefore be locally rejected
    for ~0.2s even though its endpoint quota is free. Hide only that implementation
    detail inside this bounded workflow; never wait through upstream 429s or a
    real endpoint quota window.
    """
    result: dict = {}
    for attempt in range(4):
        result = await client.request(
            "GET", "seller-analytics-api.wildberries.ru", path,
            operation_id=operation_id,
            rate_limit="4 req/hour",
            rate_scope=rate_scope,
        )
        if result.get("ok"):
            return result
        retry_after = float(result.get("retry_after_seconds", 0) or 0)
        is_short_local_pacing = (
            result.get("error_type") == "rate_limit"
            and int(result.get("code", 0) or 0) != 429
            and 0 < retry_after <= 1.0
        )
        if not is_short_local_pacing or attempt >= 3:
            return result
        await asyncio.sleep(retry_after + 0.05)
    return result


async def _wb_get_stocks_via_report(
    nm_ids: Optional[list[int]], chrt_ids: Optional[list[int]], limit: int, offset: int,
) -> dict:
    """Fallback for Base tokens using WB's official asynchronous warehouse-remains report."""
    if chrt_ids:
        return make_error(
            "forbidden",
            "WB Base tokens can return warehouse remains through the Analytics report, "
            "but that report has no chrtId field, so chrt_ids filtering requires a "
            "Personal or Service token for the fast stocks endpoint.",
            operation_id="wb_analytics_stocks_wb_warehouses",
            retryable=False,
            details={"required_token_type": ["Personal", "Service"]},
        ) | {"required_token_type": ["Personal", "Service"]}

    creds, _source = client.config.resolve_creds()
    missing = [field for field in client.config.fields if not creds.get(field)]
    if missing:
        return make_error(
            "auth", f"Missing WB credentials: {', '.join(missing)}.",
            operation_id="wb_analytics_warehouse_remains_report", retryable=False,
        )
    cabinet_key = client._creds_key(client.config, creds)
    task_cache_key = f"marketplace-report:v1:wb:warehouse-remains:{cabinet_key}"
    task_id: Optional[str] = None
    try:
        task_id = await client.rate_controller.cache_get(task_cache_key)
    except Exception:
        task_id = None

    if not task_id:
        created = await _wb_report_request(
            "/api/v1/warehouse_remains",
            operation_id="wb_analytics_warehouse_remains_create",
            rate_scope="analytics-warehouse-remains-create",
        )
        if not created.get("ok"):
            return created
        task_id = _wb_report_task_id(created)
        if not task_id:
            return make_error(
                "schema", "WB warehouse-remains creation response had no taskId.",
                operation_id="wb_analytics_warehouse_remains_create", retryable=False,
            )
        try:
            await client.rate_controller.cache_set(task_cache_key, task_id, 7200)
        except Exception:
            pass

    status_result = await _wb_report_request(
        f"/api/v1/warehouse_remains/tasks/{task_id}/status",
        operation_id="wb_analytics_warehouse_remains_status",
        rate_scope="analytics-warehouse-remains-status",
    )
    if not status_result.get("ok"):
        status_result.setdefault("source", "wb_analytics_warehouse_remains_report")
        status_result.setdefault("report_task_id", task_id)
        return status_result
    status_body = status_result.get("data")
    state = None
    if isinstance(status_body, dict):
        nested = status_body.get("data")
        if isinstance(nested, dict):
            state = nested.get("status")
        if state is None:
            state = status_body.get("status")
    if str(state or "").lower() != "done":
        return {
            "ok": False,
            "error": "report_pending",
            "error_type": "report_pending",
            "message": (
                "WB accepted the warehouse-remains report task, but it is not ready yet. "
                "Base-token status checks are limited to one request every 15 minutes; "
                "call wb_get_stocks again after the reported delay."
            ),
            "retryable": True,
            "retry_after_seconds": 900,
            "report_task_id": task_id,
            "report_status": state,
            "source": "wb_analytics_warehouse_remains_report",
        }

    downloaded = await _wb_report_request(
        f"/api/v1/warehouse_remains/tasks/{task_id}/download",
        operation_id="wb_analytics_warehouse_remains_download",
        rate_scope="analytics-warehouse-remains-download",
    )
    if not downloaded.get("ok"):
        # WB documents HTTP 204 as a valid empty report. MarketplaceClient treats
        # every 2xx as success, but keep this guard explicit for mocked/adapted clients.
        if int(downloaded.get("status", downloaded.get("code", 0)) or 0) == 204:
            rows: object = []
        else:
            return downloaded
    else:
        rows = downloaded.get("data")
    try:
        flat = _wb_flatten_remains(rows, nm_ids)
    except ValueError as exc:
        return make_error(
            "schema", str(exc), operation_id="wb_analytics_warehouse_remains_download",
            retryable=False,
        )
    page_limit = max(1, min(int(limit), 250000))
    page = flat[int(offset): int(offset) + page_limit]
    return {
        "ok": True,
        "status": 200,
        "data": {"data": {"items": page}},
        "source": "wb_analytics_warehouse_remains_report",
        "fallback_from": "wb_analytics_stocks_wb_warehouses",
        "report_task_id": task_id,
        "total_flat_rows": len(flat),
        "offset": int(offset),
        "limit": page_limit,
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
async def wb_get_stocks(
    nm_ids: Optional[list[int]] = None,
    chrt_ids: Optional[list[int]] = None,
    limit: int = 250000,
    offset: int = 0,
) -> str:
    """Get current stock on Wildberries warehouses through Seller Analytics.

    Personal/Service tokens use WB's current fast stocks endpoint. Base tokens
    transparently use the official asynchronous warehouse-remains report instead,
    because WB restricts the fast endpoint by token type even when Analytics is present.

    Args:
        nm_ids: optional WB article ids (nmId), maximum 1000. Empty means all.
        chrt_ids: optional size ids; requires Personal/Service fast endpoint.
        limit: rows per page, 1..250000 (default 250000).
        offset: number of rows to skip for pagination.
    """
    if nm_ids is not None and len(nm_ids) > 1000:
        return _j(make_error(
            "invalid_params", "nm_ids may contain at most 1000 WB articles.",
            operation_id="wb_analytics_stocks_wb_warehouses", retryable=False,
        ))
    if chrt_ids and not nm_ids:
        return _j(make_error(
            "invalid_params", "chrt_ids can only be used together with nm_ids.",
            operation_id="wb_analytics_stocks_wb_warehouses", retryable=False,
        ))
    if offset < 0:
        return _j(make_error(
            "invalid_params", "offset must be >= 0.",
            operation_id="wb_analytics_stocks_wb_warehouses", retryable=False,
        ))

    token_type = _wb_active_token_type()
    if token_type == "base":
        return _j(await _wb_get_stocks_via_report(nm_ids, chrt_ids, limit, offset))

    body: dict[str, object] = {
        "limit": max(1, min(int(limit), 250000)),
        "offset": int(offset),
    }
    if nm_ids is not None:
        body["nmIds"] = nm_ids
    if chrt_ids is not None:
        body["chrtIds"] = chrt_ids

    result = await client.request(
        "POST",
        "seller-analytics-api.wildberries.ru",
        "/api/analytics/v1/stocks-report/wb-warehouses",
        json_body=body,
        operation_id="wb_analytics_stocks_wb_warehouses",
        rate_limit="3 req/min",
        rate_scope="analytics",
    )
    if _wb_stocks_needs_report_fallback(result):
        return _j(await _wb_get_stocks_via_report(nm_ids, chrt_ids, limit, offset))
    return _j(result)


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
