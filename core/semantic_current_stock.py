"""Seller-aware execution for the approved Wildberries current-stock metric.

This module is deliberately CURRENT-STATE only.  It never treats a live stock
snapshot as historical inventory and it never reads weekly-finance archive rows
for stock truth.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .errors import make_error
from .metric_observation import build_metric_observation
from .order_history import resolve_history_cabinet

FAST_OPERATION = "wb_analytics_stocks_wb_warehouses"
FAST_HOST = "seller-analytics-api.wildberries.ru"
FAST_PATH = "/api/analytics/v1/stocks-report/wb-warehouses"
REPORT_SOURCE = "wb_analytics_warehouse_remains_report"
MAX_ROWS = 250_000


class SemanticCurrentStockExecutionError(RuntimeError):
    """Raised when a current-stock business request has unsafe semantics."""


def _token_type(token: str) -> str:
    try:
        payload = str(token).split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        return {1: "base", 2: "test", 3: "personal", 4: "service"}.get(
            int(claims.get("acc", 0) or 0), "unknown"
        )
    except (IndexError, ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return "unknown"


def _needs_report_fallback(result: dict[str, Any]) -> bool:
    if int(result.get("code", 0) or 0) != 403:
        return False
    text = " ".join(str(result.get(key, "")) for key in ("message", "details", "error"))
    return "token does not satisfy additional requirements" in text.lower()


def _report_task_id(result: dict[str, Any]) -> Optional[str]:
    body = result.get("data")
    if not isinstance(body, dict):
        return None
    nested = body.get("data")
    if isinstance(nested, dict) and nested.get("taskId"):
        return str(nested["taskId"])
    if body.get("taskId"):
        return str(body["taskId"])
    return None


async def _report_request(
    wb: Any,
    creds: dict[str, str],
    path: str,
    *,
    operation_id: str,
    rate_scope: str,
) -> dict[str, Any]:
    """Absorb only tiny local pacing between sequential report requests."""
    result: dict[str, Any] = {}
    for attempt in range(4):
        result = await wb.client.request(
            "GET",
            FAST_HOST,
            path,
            operation_id=operation_id,
            rate_limit="4 req/hour",
            rate_scope=rate_scope,
            creds_override=creds,
        )
        if result.get("ok"):
            return result
        retry_after = float(result.get("retry_after_seconds", 0) or 0)
        short_local_pacing = (
            result.get("error_type") == "rate_limit"
            and int(result.get("code", 0) or 0) != 429
            and 0 < retry_after <= 1.0
        )
        if not short_local_pacing or attempt >= 3:
            return result
        await asyncio.sleep(retry_after + 0.05)
    return result


def _flatten_report_rows(rows: object, nm_ids: Optional[list[int]]) -> list[dict[str, Any]]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise SemanticCurrentStockExecutionError("WB warehouse-remains report is not an array")
    wanted = set(nm_ids or [])
    out: list[dict[str, Any]] = []
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


async def _fetch_report_fallback(
    wb: Any,
    creds: dict[str, str],
    *,
    nm_ids: Optional[list[int]],
) -> dict[str, Any]:
    try:
        cabinet_key = wb.client._quota_key(wb.client.config, creds)
    except ValueError as exc:
        return make_error("rate_limit", str(exc), operation_id=REPORT_SOURCE, retryable=False)

    task_cache_key = f"marketplace-report:v1:wb:warehouse-remains:{cabinet_key}"
    task_id: Optional[str] = None
    try:
        task_id = await wb.client.rate_controller.cache_get(task_cache_key)
    except Exception:
        task_id = None

    if not task_id:
        created = await _report_request(
            wb,
            creds,
            "/api/v1/warehouse_remains",
            operation_id="wb_analytics_warehouse_remains_create",
            rate_scope="analytics-warehouse-remains-create",
        )
        if not created.get("ok"):
            return created
        task_id = _report_task_id(created)
        if not task_id:
            return make_error(
                "provider_data_conflict",
                "WB warehouse-remains creation response had no taskId.",
                operation_id="wb_analytics_warehouse_remains_create",
                retryable=False,
            )
        try:
            await wb.client.rate_controller.cache_set(task_cache_key, task_id, 7200)
        except Exception:
            pass

    status_result = await _report_request(
        wb,
        creds,
        f"/api/v1/warehouse_remains/tasks/{task_id}/status",
        operation_id="wb_analytics_warehouse_remains_status",
        rate_scope="analytics-warehouse-remains-status",
    )
    if not status_result.get("ok"):
        status_result.setdefault("source", REPORT_SOURCE)
        status_result.setdefault("report_task_id", task_id)
        return status_result

    body = status_result.get("data")
    state = None
    if isinstance(body, dict):
        nested = body.get("data")
        if isinstance(nested, dict):
            state = nested.get("status")
        if state is None:
            state = body.get("status")
    if str(state or "").lower() != "done":
        return {
            "ok": False,
            "error": "report_pending",
            "error_type": "report_pending",
            "message": "WB accepted the current warehouse-remains report, but it is not ready yet.",
            "retryable": True,
            "retry_after_seconds": 900,
            "report_task_id": task_id,
            "report_status": state,
            "source": REPORT_SOURCE,
        }

    downloaded = await _report_request(
        wb,
        creds,
        f"/api/v1/warehouse_remains/tasks/{task_id}/download",
        operation_id="wb_analytics_warehouse_remains_download",
        rate_scope="analytics-warehouse-remains-download",
    )
    if not downloaded.get("ok"):
        if int(downloaded.get("status", downloaded.get("code", 0)) or 0) == 204:
            rows: object = []
        else:
            return downloaded
    else:
        rows = downloaded.get("data")

    try:
        items = _flatten_report_rows(rows, nm_ids)
    except SemanticCurrentStockExecutionError as exc:
        return make_error(
            "provider_data_conflict",
            str(exc),
            operation_id="wb_analytics_warehouse_remains_download",
            retryable=False,
        )
    return {
        "ok": True,
        "source": REPORT_SOURCE,
        "fallback_from": FAST_OPERATION,
        "report_task_id": task_id,
        "items": items,
    }


def _extract_fast_items(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    body = result.get("data")

    # Production WB can return HTTP 204 No Content when the requested current
    # stock selection has no rows. MarketplaceClient correctly treats every 2xx
    # response as successful, so preserve that semantic here: an actually empty
    # 204 body is an empty stock row set, not a provider schema conflict.
    status = int(result.get("status", 0) or 0)
    if status == 204 and (body is None or (isinstance(body, str) and not body.strip())):
        return []

    if not isinstance(body, dict):
        return None
    nested = body.get("data")
    if isinstance(nested, dict) and isinstance(nested.get("items"), list):
        return nested["items"]
    if isinstance(body.get("items"), list):
        return body["items"]
    return None


async def _fetch_current_rows(
    wb: Any,
    *,
    seller: str,
    nm_ids: Optional[list[int]],
) -> tuple[str, dict[str, Any]]:
    cabinet, creds, error = resolve_history_cabinet(wb, seller)
    if error:
        return cabinet, error
    assert creds is not None

    if nm_ids is not None and len(nm_ids) > 1000:
        return cabinet, make_error(
            "invalid_params",
            "nm_ids may contain at most 1000 WB articles.",
            operation_id=FAST_OPERATION,
            retryable=False,
        )

    if _token_type(str(creds.get("token", ""))) == "base":
        return cabinet, await _fetch_report_fallback(wb, creds, nm_ids=nm_ids)

    body: dict[str, Any] = {"limit": MAX_ROWS, "offset": 0}
    if nm_ids is not None:
        body["nmIds"] = nm_ids
    result = await wb.client.request(
        "POST",
        FAST_HOST,
        FAST_PATH,
        json_body=body,
        operation_id=FAST_OPERATION,
        rate_limit="3 req/min",
        rate_scope="analytics",
        creds_override=creds,
    )
    if _needs_report_fallback(result):
        return cabinet, await _fetch_report_fallback(wb, creds, nm_ids=nm_ids)
    if not result.get("ok"):
        return cabinet, result

    items = _extract_fast_items(result)
    if items is None:
        return cabinet, make_error(
            "provider_data_conflict",
            "WB current-stock response has no data.items array.",
            operation_id=FAST_OPERATION,
            retryable=False,
        )
    if len(items) >= MAX_ROWS:
        return cabinet, make_error(
            "execution_pending",
            "WB current-stock response reached the maximum page size; no partial stock total is returned as final.",
            operation_id=FAST_OPERATION,
            retryable=True,
            details={"rows_received": len(items), "complete": False},
        )
    return cabinet, {"ok": True, "source": FAST_OPERATION, "items": items}


def _quantity(row: dict[str, Any], index: int) -> Decimal:
    if "quantity" not in row:
        raise SemanticCurrentStockExecutionError(f"WB current-stock row #{index} has no quantity")
    try:
        value = Decimal(str(row.get("quantity")))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SemanticCurrentStockExecutionError(
            f"WB current-stock row #{index} has invalid quantity"
        ) from exc
    return value


def _number(value: Decimal) -> int | float:
    return int(value) if value == value.to_integral_value() else float(value)


def _optional_decimal(
    row: dict[str, Any],
    field: str,
    index: int,
) -> Decimal | None:
    if field not in row:
        return None
    try:
        return Decimal(str(row.get(field) or 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SemanticCurrentStockExecutionError(
            f"WB current-stock row #{index} has invalid {field}"
        ) from exc


def _aggregate(items: list[dict[str, Any]], grouping: str) -> dict[str, Any]:
    total = Decimal("0")
    in_way_to_client = Decimal("0")
    in_way_from_client = Decimal("0")
    transit_fields_complete = True
    grouped: dict[str, Decimal] = {}
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            raise SemanticCurrentStockExecutionError(f"WB current-stock row #{index} is not an object")
        quantity = _quantity(raw, index)
        total += quantity

        to_client = _optional_decimal(raw, "inWayToClient", index)
        from_client = _optional_decimal(raw, "inWayFromClient", index)
        if to_client is None or from_client is None:
            transit_fields_complete = False
        else:
            in_way_to_client += to_client
            in_way_from_client += from_client

        if grouping == "PRODUCT":
            key = raw.get("nmId")
            if key is None:
                raise SemanticCurrentStockExecutionError(
                    f"WB current-stock row #{index} has no nmId required for product grouping"
                )
            group_key = str(key)
        elif grouping == "WAREHOUSE":
            key = raw.get("warehouseName")
            if key is None:
                raise SemanticCurrentStockExecutionError(
                    f"WB current-stock row #{index} has no warehouseName required for warehouse grouping"
                )
            group_key = str(key)
        else:
            continue
        grouped[group_key] = grouped.get(group_key, Decimal("0")) + quantity

    result: dict[str, Any] = {
        "stock_units": _number(total),
        "cabinet_label_ru": "Остатки «Склад WB»",
        "transit_data_available": transit_fields_complete,
    }
    if transit_fields_complete:
        result["in_way_to_client_units"] = _number(in_way_to_client)
        result["in_way_from_client_units"] = _number(in_way_from_client)
        result["in_transit_units"] = _number(in_way_to_client + in_way_from_client)
        result["in_transit"] = {
            "label_ru": "Товары в пути",
            "units": _number(in_way_to_client + in_way_from_client),
            "to_client": {
                "label_ru": "Едут к покупателю",
                "units": _number(in_way_to_client),
            },
            "from_client": {
                "label_ru": "Возвращаются на склад",
                "units": _number(in_way_from_client),
            },
        }

    if grouping == "PRODUCT":
        result["by_product"] = [
            {"nm_id": key, "stock_units": _number(value)}
            for key, value in sorted(grouped.items(), key=lambda item: item[0])
        ]
    elif grouping == "WAREHOUSE":
        result["by_warehouse"] = [
            {"warehouse": key, "stock_units": _number(value)}
            for key, value in sorted(grouped.items(), key=lambda item: item[0])
        ]
    return result


async def execute_current_stock_question(
    wb: Any,
    *,
    seller: str,
    date_from: str,
    date_to: str,
    grouping: str = "TOTAL",
    nm_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    """Execute CURRENT_STOCK only for today's current snapshot.

    An explicit current-snapshot request does not need date_from/date_to. When
    both are omitted, today's live snapshot is used. If a caller supplies dates,
    both must be present and both must equal today; any historical date remains
    fail-closed.
    """
    raw_from = str(date_from or "").strip()
    raw_to = str(date_to or "").strip()
    today = date.today()

    if not raw_from and not raw_to:
        start = today
        end = today
    elif not raw_from or not raw_to:
        raise SemanticCurrentStockExecutionError(
            "date_from and date_to must either both be omitted for the current snapshot "
            "or both be YYYY-MM-DD"
        )
    else:
        try:
            start = date.fromisoformat(raw_from[:10])
            end = date.fromisoformat(raw_to[:10])
        except (TypeError, ValueError) as exc:
            raise SemanticCurrentStockExecutionError(
                "date_from/date_to must be YYYY-MM-DD"
            ) from exc

    if start != today or end != today:
        return make_error(
            "source_not_suitable",
            "Current WB stock is a live snapshot only. Historical stock must use a separately approved historical source; the server will not substitute today's snapshot.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "metric": "CURRENT_STOCK",
                "requested_date_from": start.isoformat(),
                "requested_date_to": end.isoformat(),
                "current_date": today.isoformat(),
                "required_source_id": "wb_historical_stock",
                "rejected_substitute": "wb_current_stocks",
            },
        )

    grouping = str(grouping or "TOTAL").upper()
    if grouping not in {"TOTAL", "PRODUCT", "WAREHOUSE"}:
        return make_error(
            "source_not_suitable",
            f"CURRENT_STOCK grouping {grouping!r} is not approved.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    cabinet, fetched = await _fetch_current_rows(wb, seller=seller, nm_ids=nm_ids)
    if not fetched.get("ok"):
        return fetched
    items = fetched.get("items")
    if not isinstance(items, list):
        return make_error(
            "provider_data_conflict",
            "WB current-stock executor did not receive a row array.",
            operation_id="marketplace_business_query",
            retryable=False,
        )
    try:
        totals = _aggregate(items, grouping)
    except SemanticCurrentStockExecutionError as exc:
        return make_error(
            "provider_data_conflict",
            str(exc),
            operation_id="marketplace_business_query",
            retryable=False,
        )

    result = {
        "ok": True,
        "metric": "CURRENT_STOCK",
        "marketplace": "WB",
        "seller": cabinet,
        "as_of_date": today.isoformat(),
        "grouping": grouping,
        "provider_rows_received": len(items),
        "source": "wb_current_stocks",
        "source_operation": fetched.get("source", FAST_OPERATION),
        "data_class": "CURRENT_OPERATIONAL_STOCK",
        "complete": True,
        "semantic_rule": "sum provider quantity across the current WB warehouse stock snapshot",
        "quality": (
            "Current WB warehouse stock snapshot only. It is not a historical stock series and "
            "must not be used to answer inventory-at-past-date questions. "
            "When provider fields inWayToClient/inWayFromClient are present, transit is reported "
            "separately and is never added to «Остатки “Склад WB”»."
        ),
        **totals,
    }
    observation = build_metric_observation(
        metric_id="CURRENT_STOCK",
        value=result["stock_units"],
        unit="UNITS",
        marketplace="wb",
        source_name="wb_current_stocks",
        source_field="quantity",
        observed_at=today.isoformat(),
    ).to_dict()
    observation["dimensions"] = {
        "seller": cabinet,
        "grouping": grouping,
        "nm_ids": list(nm_ids or []),
    }
    result["metric_observations"] = [observation]
    result["knowledge_catalog_version"] = "marketplace_knowledge_catalog.v1"
    return result
