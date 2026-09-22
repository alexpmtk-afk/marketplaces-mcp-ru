"""Semantic business-query entry point with domain-specific executors."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Optional

from . import request_source_system_map as _request_source_system_map  # noqa: F401
from .business_router import execute_business_query as execute_legacy_business_query
from .errors import make_error
from .metric_registry import resolve_metric_terms
from .request_execution_controller import register_request_execution_controller_tool
from .request_join_controller import register_request_join_controller_tool
from .request_source_router import register_request_source_router_tool
from .semantic_advertising import (
    SemanticAdvertisingExecutionError,
    execute_semantic_advertising_question,
)
from .semantic_current_stock import (
    SemanticCurrentStockExecutionError,
    execute_current_stock_question,
)
from .semantic_ozon_snapshot import (
    SemanticOzonSnapshotError,
    execute_ozon_current_snapshot,
    requested_snapshot_metrics,
)
from .semantic_resolver import resolve_semantic_question
from .user_facing import present_business_result


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _resolution_with_period(
    resolution: dict[str, Any], *, date_from: str, date_to: str,
) -> dict[str, Any]:
    enriched = deepcopy(resolution)
    normalized = dict(enriched.get("normalized_query") or {})
    normalized["period"] = {"date_from": date_from, "date_to": date_to}
    enriched["normalized_query"] = normalized
    return enriched


def _attach_semantic_context(
    result: dict[str, Any], *, question: str, resolution: dict[str, Any],
) -> dict[str, Any]:
    result["semantic_question"] = question
    result["semantic_resolution"] = resolution
    result["normalized_query"] = deepcopy(resolution.get("normalized_query"))
    return result


def _marketplace_from_request(marketplace: str, question: str) -> str:
    explicit = str(marketplace or "").strip().lower()
    if explicit in {"ozon", "озон"}:
        return "ozon"
    if explicit in {"wb", "wildberries", "вайлдберриз"}:
        return "wb"
    text = str(question or "").casefold().replace("ё", "е")
    if "ozon" in text or "озон" in text:
        return "ozon"
    if "wildberries" in text or "вайлдберриз" in text or " wb " in f" {text} ":
        return "wb"
    return explicit


def _ozon_snapshot_resolution(question: str, metrics: list[str]) -> dict[str, Any]:
    dictionary = {item["metric_id"]: item for item in resolve_metric_terms(question)}
    canonical = [deepcopy(dictionary[metric]) for metric in metrics if metric in dictionary]
    target_type = "BUSINESS_METRIC" if len(metrics) == 1 else "BUSINESS_METRIC_SET"
    result: dict[str, Any] = {
        "resolution_type": target_type,
        "execution_allowed": True,
        "status": "AVAILABLE_WITH_LIMITATION" if "CURRENT_SELLING_PRICE" in metrics else "AVAILABLE",
        "route_id": "ozon_current_snapshot",
        "source_ids": [
            source for source in (
                "ozon_current_prices" if "CURRENT_SELLING_PRICE" in metrics else "",
                "ozon_current_stocks" if "CURRENT_STOCK" in metrics else "",
            ) if source
        ],
        "canonical_metrics": canonical,
        "normalized_query": {
            "marketplace": "ozon",
            "temporal_class": "CURRENT_SNAPSHOT",
            "period": None,
            "metrics": list(metrics),
        },
        "guardrail": (
            "Only the approved live Ozon current-price/current-stock snapshot may execute. "
            "Historical substitution, active-cabinet fallback, fuzzy product substitution and partial answers are forbidden."
        ),
    }
    if len(metrics) == 1:
        result["metric_id"] = metrics[0]
        result["canonical_metric"] = canonical[0] if canonical else None
    else:
        result["metric_ids"] = list(metrics)
    return result


async def execute_business_query(
    modules: dict[str, Any],
    *,
    marketplace: str = "",
    seller: str = "",
    date_from: str = "",
    date_to: str = "",
    metric: str = "",
    question: str = "",
    nm_ids: Optional[list[int]] = None,
    product_ids: Optional[list[str]] = None,
) -> dict:
    """Resolve natural wording first, then execute only an approved route."""
    natural_question = str(question or "").strip()
    marketplace_key = _marketplace_from_request(marketplace, natural_question)

    # Ozon current snapshot is intentionally resolved before legacy period
    # parsing. CURRENT_SNAPSHOT metrics do not require fake date_from/date_to.
    if natural_question and marketplace_key == "ozon":
        snapshot_metrics = requested_snapshot_metrics(natural_question)
        if snapshot_metrics:
            ozon = modules.get("ozon")
            resolution = _ozon_snapshot_resolution(natural_question, snapshot_metrics)
            if ozon is None:
                return make_error(
                    "source_not_suitable",
                    "Ozon runtime module is required for the approved current snapshot metrics.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={"question": natural_question, "semantic_resolution": resolution},
                )
            try:
                result = await execute_ozon_current_snapshot(
                    ozon,
                    question=natural_question,
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    product_ids=product_ids,
                )
            except SemanticOzonSnapshotError as exc:
                result = make_error(
                    "source_not_suitable",
                    str(exc),
                    operation_id="marketplace_business_query",
                    retryable=False,
                )
            if isinstance(result, dict):
                return _attach_semantic_context(
                    result, question=natural_question, resolution=resolution,
                )
            return result

        # Do not fall into legacy Ozon routing/date parsing for unsupported
        # wording. The current vertical slice remains deliberately fail-closed.
        return make_error(
            "source_not_suitable",
            "The Ozon question is outside the currently approved server-side business executors.",
            operation_id="marketplace_business_query",
            retryable=False,
            details={
                "question": natural_question,
                "semantic_status": "REQUIRES_OTHER_SOURCE_OR_EXECUTOR",
                "approved_ozon_metrics": ["CURRENT_SELLING_PRICE", "CURRENT_STOCK"],
            },
        )

    if natural_question and marketplace_key in {"wb", "wildberries"}:
        resolution = _resolution_with_period(
            resolve_semantic_question(natural_question),
            date_from=date_from,
            date_to=date_to,
        )

        if resolution.get("resolution_type") == "BUSINESS_METRIC":
            metric_id = str(resolution.get("metric_id") or "")
            if resolution.get("execution_allowed") is not True:
                return make_error(
                    "source_not_suitable",
                    "The business metric was understood, but the requested grouping or filter does not yet have an approved execution contract.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                    },
                )

            if metric_id == "ORDERS":
                result = await execute_legacy_business_query(
                    modules,
                    marketplace=marketplace or "wb",
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    metric="ORDERS",
                    question="",
                    nm_ids=nm_ids,
                )
                if isinstance(result, dict):
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result

            if metric_id == "CURRENT_STOCK":
                wb = modules.get("wb")
                if wb is None:
                    return make_error(
                        "source_not_suitable",
                        "Wildberries runtime module is required for the approved current-stock metric.",
                        operation_id="marketplace_business_query",
                        retryable=False,
                        details={
                            "question": natural_question,
                            "semantic_resolution": resolution,
                        },
                    )
                try:
                    result = await execute_current_stock_question(
                        wb,
                        seller=seller,
                        date_from=date_from,
                        date_to=date_to,
                        grouping=str(
                            (resolution.get("normalized_query") or {}).get("grouping") or "TOTAL"
                        ),
                        nm_ids=nm_ids,
                    )
                except SemanticCurrentStockExecutionError as exc:
                    result = make_error(
                        "invalid_params",
                        str(exc),
                        operation_id="marketplace_business_query",
                        retryable=False,
                    )
                if isinstance(result, dict):
                    return _attach_semantic_context(
                        result, question=natural_question, resolution=resolution,
                    )
                return result

            return make_error(
                "source_not_suitable",
                f"Business metric {metric_id!r} is registered but has no approved runtime executor.",
                operation_id="marketplace_business_query",
                retryable=False,
                details={
                    "question": natural_question,
                    "semantic_resolution": resolution,
                },
            )

        if (
            resolution.get("resolution_type") == "CAPABILITY"
            and resolution.get("capability_id") == "advertising_performance"
        ):
            store = modules.get("_archive_store")
            if store is None:
                return make_error(
                    "source_not_suitable",
                    "Canonical marketplace archive storage is required for historical advertising semantics.",
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                        "required": "canonical Google Drive advertising archive",
                    },
                )
            try:
                result = await execute_semantic_advertising_question(
                    store,
                    question=natural_question,
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    nm_ids=nm_ids,
                )
            except SemanticAdvertisingExecutionError as exc:
                text = str(exc)
                lowered = text.lower()
                if "full_coverage" in lowered or "coverage" in lowered:
                    error_type = "coverage_gap"
                elif "yyyy-mm-dd" in lowered or "date_from" in lowered or "date_to" in lowered:
                    error_type = "invalid_params"
                else:
                    error_type = "source_not_suitable"
                return make_error(
                    error_type,
                    text,
                    operation_id="marketplace_business_query",
                    retryable=False,
                    details={
                        "question": natural_question,
                        "semantic_resolution": resolution,
                        "capability_id": "advertising_performance",
                    },
                )
            return _attach_semantic_context(
                result, question=natural_question, resolution=resolution,
            )

    return await execute_legacy_business_query(
        modules,
        marketplace=marketplace,
        seller=seller,
        date_from=date_from,
        date_to=date_to,
        metric=metric,
        question=question,
        nm_ids=nm_ids,
    )


def register_business_query_tool(combined: Any, modules: dict[str, Any]) -> None:
    """Register planning, execution, join control, and business execution."""

    # The planner is always first. Execution Controller owns exact per-leg calls;
    # Join Controller later validates completed leg results before synthesis/math.
    register_request_source_router_tool(combined)
    register_request_execution_controller_tool(combined)
    register_request_join_controller_tool(combined)

    @combined.tool(
        name="marketplace_business_query",
        annotations={
            "title": "Marketplace business query (semantic server-side routing)",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def marketplace_business_query(
        question: str = "",
        marketplace: str = "",
        seller: str = "",
        date_from: str = "",
        date_to: str = "",
        metric: str = "",
        nm_ids: Optional[list[int]] = None,
        product_ids: Optional[list[str]] = None,
    ) -> str:
        try:
            result = await execute_business_query(
                modules,
                marketplace=marketplace,
                seller=seller,
                date_from=date_from,
                date_to=date_to,
                question=question,
                metric=metric,
                nm_ids=nm_ids,
                product_ids=product_ids,
            )
        except Exception as exc:  # last-resort user-facing boundary; diagnostics stay technical
            technical = str(exc)
            lowered = technical.casefold()
            archive_failure = any(marker in lowered for marker in (
                "archive",
                "google drive",
                "read_range_by_id",
                "historical database",
                "historical store",
            ))
            result = make_error(
                "source_not_suitable" if archive_failure else "server_error",
                technical,
                operation_id="marketplace_business_query",
                retryable=not archive_failure,
                details={
                    "question": question,
                    "required": "historical database" if archive_failure else "marketplace runtime",
                    "exception_type": type(exc).__name__,
                },
            )
        if isinstance(result, dict):
            result = present_business_result(
                result,
                question=question,
                marketplace=marketplace,
                seller=seller,
            )
        return _j(result)
