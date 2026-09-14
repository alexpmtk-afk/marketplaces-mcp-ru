"""Server-side business query routing for marketplace analytics.

Clients may provide the original business question. The server resolves meaning,
selects only approved sources, checks coverage and performs aggregation.
Legacy metric routing remains for compatibility, but it must not override the
semantic meaning of a natural-language question.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Optional

from .errors import make_error
from .order_history import (
    OrderHistoryStore,
    canonical_order_totals,
    resolve_history_cabinet,
)
from .semantic_archive import (
    SemanticArchiveExecutionError,
    execute_semantic_archive_question,
    load_semantic_execution,
)
from .semantic_resolver import resolve_semantic_question

# WB Statistics Orders is an official operational/preliminary source. WB states
# that it may omit orders with unconfirmed/delayed/installment payments and
# directs users to Order Feed for the complete order flow. Therefore it must
# never be presented as complete marketplace-order truth.
WB_OPERATIONAL_RETENTION_DAYS = 90
WB_STATS_MAX_ROWS = 80_000


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _parse_day(value: str, field: str) -> date:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date (YYYY-MM-DD)") from exc


def _semantic_error(
    message: str,
    *,
    resolution: dict[str, Any],
    question: str,
    error_type: str = "source_not_suitable",
    extra: dict[str, Any] | None = None,
) -> dict:
    details: dict[str, Any] = {
        "question": question,
        "semantic_resolution": resolution,
    }
    if extra:
        details.update(extra)
    return make_error(
        error_type,
        message,
        operation_id="marketplace_business_query",
        retryable=False,
        details=details,
    )


async def _route_natural_question(
    modules: dict[str, Any],
    *,
    marketplace_key: str,
    question: str,
    seller: str,
    start: date,
    end: date,
    nm_ids: Optional[list[int]],
) -> dict:
    """Route the user's original wording through Semantic Core first."""
    if marketplace_key not in {"wb", "wildberries"}:
        return make_error(
            "source_not_suitable",
            "Natural-language business routing is not yet executable for this marketplace.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "question": question,
                "marketplace": marketplace_key,
                "semantic_status": "REQUIRES_OTHER_SOURCE",
                "required_source_id": "ozon_reports" if marketplace_key in {"ozon", "озон"} else None,
            },
        )

    resolution = resolve_semantic_question(question)
    if resolution.get("resolution_type") != "CAPABILITY":
        return _semantic_error(
            resolution.get("reason")
            or resolution.get("guardrail")
            or "The question cannot be safely answered from an approved current source.",
            resolution=resolution,
            question=question,
        )

    capability_id = str(resolution.get("capability_id") or "")
    execution = load_semantic_execution()
    if capability_id not in execution["executors"]:
        return _semantic_error(
            (
                f"The question was understood as {capability_id!r}, but this capability "
                "does not yet have an approved executable calculation contract."
            ),
            resolution=resolution,
            question=question,
            extra={"capability_id": capability_id},
        )

    archive_store = modules.get("_archive_store")
    if archive_store is None:
        return _semantic_error(
            "The approved semantic calculation requires the canonical marketplace archive, but archive storage is not configured.",
            resolution=resolution,
            question=question,
            extra={"capability_id": capability_id, "required": "canonical Google Drive archive"},
        )

    try:
        result = await execute_semantic_archive_question(
            archive_store,
            question=question,
            seller=seller,
            date_from=start,
            date_to=end,
            nm_ids=nm_ids,
        )
    except SemanticArchiveExecutionError as exc:
        return _semantic_error(
            str(exc),
            resolution=resolution,
            question=question,
            error_type="invalid_params",
            extra={"capability_id": capability_id},
        )

    if isinstance(result, dict):
        result.setdefault("route", "semantic_archive")
        result["semantic_question"] = question
        result["semantic_resolution"] = resolution
    return result


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

    # WB documents a response ceiling for this Statistics feed. If the ceiling
    # is reached, a continuation by lastChangeDate is required. Never present
    # the first page as a complete aggregate.
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

    try:
        totals = canonical_order_totals(rows, start, end)
    except ValueError as exc:
        return make_error(
            "provider_data_conflict",
            str(exc),
            operation_id="wb_stats_orders",
            retryable=False,
        )
    return {
        "ok": True,
        "metric": "ORDERS",
        "marketplace": "WB",
        "seller": seller,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "provider_rows_received": len(rows),
        "route": "operational_range",
        "source": "wb_stats_orders",
        "source_operation": "wb_stats_orders",
        "complete": True,
        "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
        "semantic_rule": "count rows where isCancel=false; sum finishedPrice",
        "source_validation": "official_operational_preliminary",
        "quality": (
            "Official WB operational Statistics Orders feed. WB documents that this source "
            "may omit orders with unconfirmed, delayed or installment payments; it must not "
            "be presented as the complete marketplace order flow."
        ),
        **totals,
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
            "WB Statistics Orders does not provide the requested historical depth.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "requested_date_from": start.isoformat(),
                "oldest_operational_date": oldest_supported.isoformat(),
                "canonical_source": "wb_stats_orders",
                "source_limitations": "official operational/preliminary feed; not complete order flow",
            },
        )

    cabinet, creds, error = resolve_history_cabinet(wb, seller)
    if error:
        return error
    assert creds is not None

    spec = wb.catalog.get("wb_stats_orders")
    if spec is None:
        return make_error(
            "source_not_suitable",
            "WB Statistics Orders route is absent from the runtime catalog.",
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
        response.get("data"), start=start, end=end, seller=cabinet,
    )


async def _wb_orders_period_aware(
    wb: Any,
    history_store: OrderHistoryStore | None,
    *,
    seller: str,
    start: date,
    end: date,
) -> dict:
    """Use the operational feed and its stored history without claiming full-order truth."""
    today = date.today()
    if end > today:
        return make_error("invalid_params", "date_to cannot be in the future", retryable=False)

    oldest_live = today - timedelta(days=WB_OPERATIONAL_RETENTION_DAYS - 1)
    if start >= oldest_live:
        return await _wb_orders_operational_range(
            wb, seller=seller, start=start, end=end,
        )

    cabinet, _creds, error = resolve_history_cabinet(wb, seller)
    if error:
        return error

    historical_end = min(end, oldest_live - timedelta(days=1))
    if history_store is None:
        return make_error(
            "source_not_suitable",
            "The requested WB ORDERS period is older than the Statistics window and the selective Historical Store is not configured.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "cabinet": cabinet,
                "requested_date_from": start.isoformat(),
                "historical_segment_to": historical_end.isoformat(),
                "canonical_source": "wb_stats_orders",
                "source_limitations": "official operational/preliminary feed; not complete order flow",
                "rejected_substitute": "wb_analytics_funnel",
                "rejected_reason": "live parity mismatch on all three TEST WB cabinets (2026-09-11)",
                "requires": "MARKETPLACE_MCP_YDB_ENDPOINT + verified WB ORDERS history coverage",
            },
        )

    coverage = history_store.coverage(cabinet)
    if not coverage.covers(start, historical_end):
        return make_error(
            "coverage_gap",
            "The selective WB ORDERS Historical Store does not fully cover the requested old segment. The server will not fill the gap with a semantically different report.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "cabinet": cabinet,
                "requested_from": start.isoformat(),
                "required_historical_to": historical_end.isoformat(),
                "history_status": coverage.status,
                "covered_from": coverage.covered_from,
                "covered_to": coverage.covered_to,
            },
        )

    historical_rows = history_store.read_rows(cabinet, start, historical_end)
    try:
        historical_totals = canonical_order_totals(historical_rows, start, historical_end)
    except ValueError as exc:
        return make_error(
            "provider_data_conflict",
            f"Historical WB ORDERS data failed validation: {exc}",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    if end <= historical_end:
        return {
            "ok": True,
            "metric": "ORDERS",
            "marketplace": "WB",
            "seller": cabinet,
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "route": "historical_store",
            "source": "wb_orders_history_ydb",
            "source_validation": "historical_copy_of_operational_preliminary_feed",
            "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
            "storage_scope": history_store.storage_scope,
            "history_covered_from": coverage.covered_from,
            "history_covered_to": coverage.covered_to,
            "complete": True,
            "semantic_rule": "count rows where isCancel=false; sum finishedPrice",
            **historical_totals,
        }

    current_start = oldest_live
    current = await _wb_orders_operational_range(
        wb, seller=seller, start=current_start, end=end,
    )
    if not current.get("ok"):
        failed = dict(current)
        failed["partial_history_discarded"] = True
        failed["historical_segment"] = [start.isoformat(), historical_end.isoformat()]
        failed["complete"] = False
        return failed

    return {
        "ok": True,
        "metric": "ORDERS",
        "marketplace": "WB",
        "seller": cabinet,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "route": "historical_plus_operational",
        "source": "wb_orders_history_ydb+wb_stats_orders",
        "source_validation": "operational_preliminary_split",
        "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
        "storage_scope": history_store.storage_scope,
        "history_segment": [start.isoformat(), historical_end.isoformat()],
        "operational_segment": [current_start.isoformat(), end.isoformat()],
        "history_covered_from": coverage.covered_from,
        "history_covered_to": coverage.covered_to,
        "orders_count": int(historical_totals["orders_count"]) + int(current["orders_count"]),
        "orders_amount": float(historical_totals["orders_amount"]) + float(current["orders_amount"]),
        "currency": "RUB",
        "cancelled_orders_excluded": (
            int(historical_totals["cancelled_orders_excluded"])
            + int(current["cancelled_orders_excluded"])
        ),
        "complete": True,
        "semantic_rule": "count rows where isCancel=false; sum finishedPrice",
    }


async def execute_business_query(
    modules: dict[str, Any],
    *,
    marketplace: str,
    seller: str,
    date_from: str,
    date_to: str,
    metric: str = "",
    question: str = "",
    nm_ids: Optional[list[int]] = None,
) -> dict:
    """Resolve the original business question first, or use legacy metric routing."""
    marketplace_key = marketplace.strip().lower()
    try:
        start = _parse_day(date_from, "date_from")
        end = _parse_day(date_to, "date_to")
    except ValueError as exc:
        return make_error("invalid_params", str(exc), retryable=False)
    if start > end:
        return make_error("invalid_params", "date_from must be <= date_to", retryable=False)

    natural_question = str(question or "").strip()
    if natural_question:
        return await _route_natural_question(
            modules,
            marketplace_key=marketplace_key,
            question=natural_question,
            seller=seller,
            start=start,
            end=end,
            nm_ids=nm_ids,
        )

    if marketplace_key not in {"wb", "wildberries"}:
        return make_error(
            "source_not_suitable",
            "Business Query Router currently enables automatic business routing only for Wildberries.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    metric_key = str(metric or "").strip().upper()
    if not metric_key:
        return make_error(
            "invalid_params",
            "Provide the original business question. Legacy callers may provide metric instead.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    wb = modules["wb"]
    history_store = modules.get("_order_history_store")

    if metric_key in {"ORDERS", "ORDER"}:
        if nm_ids:
            return make_error(
                "source_not_suitable",
                "Product-filtered operational ORDERS is not yet approved in the legacy Business Query Router.",
                operation_id="marketplace_business_query",
                retryable=False,
            )
        oldest_live = date.today() - timedelta(days=WB_OPERATIONAL_RETENTION_DAYS - 1)
        if start == end and start >= oldest_live:
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
                result["source_validation"] = "official_operational_preliminary"
                result["business_completeness"] = "PRELIMINARY_NOT_ALL_ORDERS"
                result["quality"] = (
                    "Official WB Statistics Orders operational data; not guaranteed to contain "
                    "the complete marketplace order flow."
                )
            return result
        return await _wb_orders_period_aware(
            wb, history_store, seller=seller, start=start, end=end,
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
            "SALES aggregation is not yet enabled in the legacy metric route. Use the natural-language question path so Semantic Core can select only approved semantics.",
            operation_id="marketplace_business_query",
            retryable=False,
        )

    return make_error(
        "source_not_suitable",
        f"Metric {metric_key!r} has no approved legacy Business Query Router route.",
        operation_id="marketplace_business_query",
        retryable=False,
    )


def register_business_query_tool(combined: Any, modules: dict[str, Any]) -> None:
    """Register the canonical server-side entry point for business routing."""

    @combined.tool(
        name="marketplace_business_query",
        annotations={
            "title": "Marketplace business query (semantic server-side routing)",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def marketplace_business_query(
        marketplace: str,
        seller: str,
        date_from: str,
        date_to: str,
        question: str = "",
        metric: str = "",
        nm_ids: Optional[list[int]] = None,
    ) -> str:
        """Primary entry point for marketplace business questions.

        Prefer ``question`` with the user's original wording. The server resolves
        meaning, chooses only an approved source, checks coverage and calculates.
        ``metric`` remains only for backward compatibility with older clients.
        A recognized concept is never silently replaced with a merely similar
        provider metric or archive field.
        """
        return _j(await execute_business_query(
            modules,
            marketplace=marketplace,
            seller=seller,
            date_from=date_from,
            date_to=date_to,
            question=question,
            metric=metric,
            nm_ids=nm_ids,
        ))
