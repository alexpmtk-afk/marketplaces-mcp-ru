"""Server-side business query routing for marketplace analytics.

The public tool accepts business intent (metric/period/cabinet), not provider
endpoint names. Source selection therefore lives on the MCP server and is
shared by every connected ChatGPT/Codex client.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .business_registry import resolve_business_cabinet
from .errors import make_error

WB_FUNNEL_MAX_DAYS = 365
WB_FUNNEL_PAGE_SIZE = 1000
WB_FUNNEL_MAX_PAGES_SYNC = 3
WB_FUNNEL_WAIT_BUDGET_SECONDS = 45.0
WB_OPERATIONAL_RETENTION_DAYS = 90


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date (YYYY-MM-DD)") from exc


def _num(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"provider returned a non-numeric amount: {value!r}") from exc


def _resolve_named_creds(service_module: Any, seller: str) -> tuple[Optional[dict[str, str]], str, Optional[dict]]:
    service = service_module.client.config.name
    business = resolve_business_cabinet(service, seller)
    credential_name = business.cabinet if business else seller.strip()
    creds, resolved = service_module.client.config.store.resolve_named(
        service,
        service_module.client.config.fields,
        service_module.client.config.env_map,
        credential_name,
    )
    missing = [field for field in service_module.client.config.fields if not creds.get(field)]
    if not resolved or missing:
        if business:
            return None, "", make_error(
                "seller_known_but_not_configured",
                f"Кабинет {business.business_entity} известен, но credentials {service.upper()} не настроены.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={
                    "seller": seller,
                    "cabinet": business.cabinet,
                    "credentials_status": "not_configured",
                    "upstream_request_sent": False,
                },
            )
        return None, "", make_error(
            "invalid_params",
            f"{service.upper()} cabinet {seller!r} was not found or is incomplete.",
            operation_id="marketplace_business_query",
            retryable=False,
        )
    return creds, resolved, None


def _funnel_items(response: dict) -> list[dict]:
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("WB Sales Funnel response is not an object")
    body = data.get("data", data)
    if not isinstance(body, dict):
        raise ValueError("WB Sales Funnel response data is not an object")
    products = body.get("products")
    if products is None:
        return []
    if not isinstance(products, list):
        raise ValueError("WB Sales Funnel data.products is not an array")
    return [row for row in products if isinstance(row, dict)]


def _aggregate_funnel_orders(products: list[dict]) -> dict:
    gross_count = 0
    cancelled_count = 0
    gross_amount = Decimal("0")
    cancelled_amount = Decimal("0")
    malformed = 0

    for row in products:
        selected = ((row.get("statistic") or {}).get("selected") or {})
        if not isinstance(selected, dict):
            malformed += 1
            continue
        try:
            gross_count += int(selected.get("orderCount") or 0)
            cancelled_count += int(selected.get("cancelCount") or 0)
            gross_amount += _num(selected.get("orderSum"))
            cancelled_amount += _num(selected.get("cancelSum"))
        except (TypeError, ValueError):
            malformed += 1

    return {
        "orders_count": gross_count - cancelled_count,
        "orders_amount": float(gross_amount - cancelled_amount),
        "gross_orders_count": gross_count,
        "gross_orders_amount": float(gross_amount),
        "cancelled_orders_count": cancelled_count,
        "cancelled_orders_amount": float(cancelled_amount),
        "currency": "RUB",
        "malformed_products_skipped": malformed,
    }


async def _call_with_short_wait(
    client: Any,
    spec: Any,
    *,
    body: dict[str, Any],
    creds: dict[str, str],
    deadline: float,
) -> dict:
    """Absorb only short, proven pacing inside a composite business query."""
    while True:
        result = await client.call_spec(spec, json_body=body, creds_override=creds)
        if result.get("ok"):
            return result
        if result.get("error_type") != "rate_limit":
            return result
        retry_after = float(
            result.get("retry_after_seconds", result.get("retry_after_sec", 0)) or 0
        )
        remaining = deadline - time.monotonic()
        if retry_after <= 0 or retry_after > 21.0 or retry_after + 0.1 > remaining:
            return result
        await asyncio.sleep(retry_after + 0.05)


async def _wb_orders_large_period(
    wb: Any,
    *,
    seller: str,
    start: date,
    end: date,
    nm_ids: Optional[list[int]],
) -> dict:
    today = date.today()
    period_days = (end - start).days + 1
    if period_days < 1:
        return make_error("invalid_params", "date_from must be <= date_to", retryable=False)
    if period_days > WB_FUNNEL_MAX_DAYS or start < today - timedelta(days=WB_FUNNEL_MAX_DAYS - 1):
        return make_error(
            "source_not_suitable",
            "WB Sales Funnel can supply at most the last 365 days. This period needs another approved historical source.",
            operation_id="wb_analytics_funnel",
            retryable=False,
            details={
                "requested_days": period_days,
                "provider_history_days": WB_FUNNEL_MAX_DAYS,
                "route": "wb_sales_funnel_products",
            },
        )
    if end > today:
        return make_error("invalid_params", "date_to cannot be in the future", retryable=False)
    if nm_ids is not None and len(nm_ids) > 1000:
        return make_error(
            "invalid_params",
            "nm_ids may contain at most 1000 WB articles for one Sales Funnel query.",
            operation_id="wb_analytics_funnel",
            retryable=False,
        )

    creds, resolved_seller, error = _resolve_named_creds(wb, seller)
    if error:
        return error
    assert creds is not None

    spec = wb.catalog.get("wb_analytics_funnel")
    if spec is None:
        return make_error(
            "source_not_suitable",
            "The approved WB Sales Funnel route is absent from the runtime catalog.",
            operation_id="wb_analytics_funnel",
            retryable=False,
        )

    products: list[dict] = []
    offset = 0
    pages = 0
    deadline = time.monotonic() + WB_FUNNEL_WAIT_BUDGET_SECONDS

    while True:
        body: dict[str, Any] = {
            "selectedPeriod": {"start": start.isoformat(), "end": end.isoformat()},
            "nmIds": list(nm_ids or []),
            "brandNames": [],
            "subjectIds": [],
            "tagIds": [],
            "skipDeletedNm": False,
            "limit": WB_FUNNEL_PAGE_SIZE,
            "offset": offset,
        }
        result = await _call_with_short_wait(
            wb.client, spec, body=body, creds=creds, deadline=deadline
        )
        if not result.get("ok"):
            if products:
                result = dict(result)
                result["partial_result_discarded"] = True
                result["partial_products_fetched"] = len(products)
                result["resume_offset"] = offset
                result["complete"] = False
            return result

        try:
            page = _funnel_items(result)
        except ValueError as exc:
            return make_error(
                "provider_data_conflict",
                str(exc),
                operation_id=spec.operation_id,
                retryable=False,
            )

        products.extend(page)
        pages += 1
        if len(page) < WB_FUNNEL_PAGE_SIZE:
            break
        offset += len(page)
        if pages >= WB_FUNNEL_MAX_PAGES_SYNC:
            return make_error(
                "execution_pending",
                "The large-period WB query needs more provider pages than can be safely completed inside one synchronous MCP call. No partial total is returned as final. A resumable task executor is required.",
                operation_id=spec.operation_id,
                retryable=True,
                details={
                    "partial_products_fetched": len(products),
                    "resume_offset": offset,
                    "complete": False,
                },
            )

    totals = _aggregate_funnel_orders(products)
    return {
        "ok": True,
        "metric": "ORDERS",
        "marketplace": "WB",
        "seller": resolved_seller,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "route": "provider_aggregate",
        "source": "wb_analytics_funnel",
        "source_operation": spec.operation_id,
        "pages_fetched": pages,
        "products_fetched": len(products),
        "complete": True,
        "semantic_rule": "orderCount - cancelCount; orderSum - cancelSum",
        "source_validation": "candidate_pending_live_parity",
        "quality": (
            "WB Sales Funnel is a provider-native aggregate with up to 365 days of history. "
            "The route is intentionally marked candidate until live parity against the canonical Statistics Orders metric is accepted."
        ),
        **totals,
    }


async def execute_business_query(
    modules: dict[str, Any],
    *,
    marketplace: str,
    metric: str,
    seller: str,
    date_from: str,
    date_to: str,
    nm_ids: Optional[list[int]] = None,
) -> dict:
    """Resolve business intent to an approved/candidate provider route."""
    marketplace_key = marketplace.strip().lower()
    metric_key = metric.strip().upper()
    try:
        start = _parse_day(date_from, "date_from")
        end = _parse_day(date_to, "date_to")
    except ValueError as exc:
        return make_error("invalid_params", str(exc), retryable=False)
    if start > end:
        return make_error("invalid_params", "date_from must be <= date_to", retryable=False)

    if marketplace_key not in {"wb", "wildberries"}:
        return make_error(
            "source_not_suitable",
            "Business Query Router v1 currently enables automatic large-period routing only for Wildberries.",
            operation_id="marketplace_business_query",
            retryable=False,
        )
    wb = modules["wb"]

    if metric_key in {"ORDERS", "ORDER"}:
        if start == end:
            raw = await wb.wb_get_orders_summary(seller, start.isoformat(), end.isoformat())
            try:
                result = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                return make_error(
                    "provider_data_conflict",
                    "wb_get_orders_summary returned a non-JSON result",
                    operation_id="wb_get_orders_summary",
                    retryable=False,
                )
            if isinstance(result, dict) and result.get("ok"):
                result["route"] = "operational_exact_day"
                result["complete"] = True
            return result
        return await _wb_orders_large_period(
            wb, seller=seller, start=start, end=end, nm_ids=nm_ids
        )

    if metric_key in {"SALES", "SALE"}:
        period_days = (end - start).days + 1
        if period_days > WB_OPERATIONAL_RETENTION_DAYS:
            return make_error(
                "source_not_suitable",
                "A current WB Finance source exists for long periods, but automatic SALES substitution is not enabled until its business semantics are validated against the approved SALES contract. The server fails closed instead of guessing.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={
                    "candidate_source": "wb_finance_sales_reports_detailed",
                    "requested_days": period_days,
                    "operational_history_days": WB_OPERATIONAL_RETENTION_DAYS,
                    "requires": "live semantic parity + Business Metrics Contract approval",
                },
            )
        return make_error(
            "source_not_suitable",
            "SALES aggregation is not yet enabled in Business Query Router v1. Use the existing approved operational SALES path until the Semantic Layer implementation is merged.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    return make_error(
        "source_not_suitable",
        f"Metric {metric_key!r} has no approved Business Query Router v1 route.",
        operation_id="marketplace_business_query",
        retryable=False,
    )


def register_business_query_tool(combined: Any, modules: dict[str, Any]) -> None:
    """Register the canonical server-side entry point for business routing."""

    @combined.tool(
        name="marketplace_business_query",
        annotations={
            "title": "Marketplace business query (server-side source routing)",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def marketplace_business_query(
        marketplace: str,
        metric: str,
        seller: str,
        date_from: str,
        date_to: str,
        nm_ids: Optional[list[int]] = None,
    ) -> str:
        """Primary entry point for business metrics and large periods.

        Clients provide business intent only. The MCP server chooses the provider
        source, period strategy, pagination and aggregation. Do not choose a raw
        WB/Ozon endpoint in the chat when this business route supports the metric.
        """
        return _j(await execute_business_query(
            modules,
            marketplace=marketplace,
            metric=metric,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            nm_ids=nm_ids,
        ))
