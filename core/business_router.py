"""Server-side business query routing for marketplace analytics.

Clients provide business intent, not provider endpoint names. Source selection,
period suitability and aggregation therefore live on the MCP server and are
shared by every connected ChatGPT/Codex client.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from .business_registry import resolve_business_cabinet
from .errors import make_error

# WB Statistics Orders is the approved canonical source for ORDERS. The
# Business Metrics Contract currently treats its practical history as ~90 days.
# Do not silently substitute Sales Funnel or Finance beyond this window.
WB_OPERATIONAL_RETENTION_DAYS = 90
WB_STATS_MAX_ROWS = 80_000


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date (YYYY-MM-DD)") from exc


def _num(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"provider returned a non-numeric amount: {value!r}") from exc


def _resolve_named_creds(
    service_module: Any, seller: str,
) -> tuple[Optional[dict[str, str]], str, Optional[dict]]:
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


def _aggregate_statistics_orders(
    rows: object, *, start: date, end: date, seller: str,
) -> dict:
    if not isinstance(rows, list):
        return make_error(
            "provider_data_conflict",
            "WB Statistics Orders response is not an array.",
            operation_id="wb_stats_orders",
            retryable=False,
        )

    # WB documents a maximum response size for this Statistics feed. If the
    # ceiling is reached, a continuation request by lastChangeDate is required.
    # That endpoint is only 1 req/min, so a synchronous MCP call must not pretend
    # the first 80k rows are complete.
    if len(rows) >= WB_STATS_MAX_ROWS:
        last_change = None
        last = rows[-1] if rows else None
        if isinstance(last, dict):
            last_change = last.get("lastChangeDate")
        return make_error(
            "execution_pending",
            "WB Statistics Orders reached the provider page ceiling. A continuation by lastChangeDate is required; no partial aggregate is returned as final.",
            operation_id="wb_stats_orders",
            retryable=True,
            details={
                "rows_received": len(rows),
                "resume_last_change_date": last_change,
                "complete": False,
            },
        )

    count = 0
    cancelled = 0
    amount = Decimal("0")
    malformed = 0
    matched_rows = 0
    for row in rows:
        if not isinstance(row, dict):
            malformed += 1
            continue
        try:
            row_day = _parse_day(str(row.get("date", "")), "WB order date")
        except ValueError:
            malformed += 1
            continue
        if row_day < start or row_day > end:
            continue
        matched_rows += 1
        if bool(row.get("isCancel", False)):
            cancelled += 1
            continue
        try:
            amount += _num(row.get("finishedPrice"))
        except ValueError:
            return make_error(
                "provider_data_conflict",
                "WB Statistics Orders row has no valid finishedPrice.",
                operation_id="wb_stats_orders",
                retryable=False,
            )
        count += 1

    return {
        "ok": True,
        "metric": "ORDERS",
        "marketplace": "WB",
        "seller": seller,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "orders_count": count,
        "orders_amount": float(amount),
        "currency": "RUB",
        "cancelled_orders_excluded": cancelled,
        "provider_rows_received": len(rows),
        "provider_rows_in_period": matched_rows,
        "malformed_rows_skipped": malformed,
        "route": "operational_range",
        "source": "wb_stats_orders",
        "source_operation": "wb_stats_orders",
        "complete": True,
        "semantic_rule": "count rows where isCancel=false; sum finishedPrice",
        "source_validation": "approved",
        "quality": (
            "Canonical WB Statistics Orders semantics from the Business Metrics Contract. "
            "Rows outside the requested calendar period are filtered server-side."
        ),
    }


async def _wb_orders_operational_range(
    wb: Any,
    *,
    seller: str,
    start: date,
    end: date,
) -> dict:
    today = date.today()
    if end > today:
        return make_error("invalid_params", "date_to cannot be in the future", retryable=False)

    oldest_supported = today - timedelta(days=WB_OPERATIONAL_RETENTION_DAYS - 1)
    if start < oldest_supported:
        return make_error(
            "source_not_suitable",
            "Canonical WB Statistics Orders does not provide the requested historical depth. Sales Funnel was live-tested and is not semantically equivalent, so the server refuses to substitute it. An approved historical ORDERS source/store is required.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "requested_date_from": start.isoformat(),
                "oldest_operational_date": oldest_supported.isoformat(),
                "canonical_source": "wb_stats_orders",
                "rejected_substitute": "wb_analytics_funnel",
                "rejected_reason": "live parity mismatch on all three TEST WB cabinets (2026-09-11)",
                "requires": "approved historical ORDERS source or selective Historical Store",
            },
        )

    creds, resolved_seller, error = _resolve_named_creds(wb, seller)
    if error:
        return error
    assert creds is not None

    spec = wb.catalog.get("wb_stats_orders")
    if spec is None:
        return make_error(
            "source_not_suitable",
            "Canonical WB Statistics Orders route is absent from the runtime catalog.",
            operation_id="wb_stats_orders",
            retryable=False,
        )

    response = await wb.client.call_spec(
        spec,
        query={"dateFrom": start.isoformat() + "T00:00:00", "flag": 0},
        creds_override=creds,
        retry_on_429=False,
    )
    if not response.get("ok"):
        return response
    return _aggregate_statistics_orders(
        response.get("data"), start=start, end=end, seller=resolved_seller,
    )


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
    """Resolve business intent to an approved provider route or fail closed."""
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
            "Business Query Router v1 currently enables automatic business routing only for Wildberries.",
            operation_id="marketplace_business_query",
            retryable=False,
        )
    wb = modules["wb"]

    if metric_key in {"ORDERS", "ORDER"}:
        if nm_ids:
            return make_error(
                "source_not_suitable",
                "Product-filtered canonical ORDERS is not yet approved in Business Query Router v1.",
                operation_id="marketplace_business_query",
                retryable=False,
            )
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
                result["source_validation"] = "approved"
            return result
        return await _wb_orders_operational_range(
            wb, seller=seller, start=start, end=end,
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
            "SALES aggregation is not yet enabled in Business Query Router v1. Use the existing approved operational SALES path until it is wired into this server-native entry point.",
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
        """Primary entry point for business metrics and period-aware routing.

        Clients provide business intent only. The MCP server chooses the approved
        source, checks historical suitability and performs aggregation. It never
        silently replaces one business metric with a merely similar provider metric.
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
