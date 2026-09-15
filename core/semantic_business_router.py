"""Semantic business-query entry point with domain-specific executors."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Optional

from .business_router import execute_business_query as execute_legacy_business_query
from .errors import make_error
from .semantic_advertising import (
    SemanticAdvertisingExecutionError,
    execute_semantic_advertising_question,
)
from .semantic_resolver import resolve_semantic_question


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
    """Resolve natural wording first, then execute only an approved route."""
    marketplace_key = str(marketplace or "").strip().lower()
    natural_question = str(question or "").strip()
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
                    marketplace=marketplace,
                    seller=seller,
                    date_from=date_from,
                    date_to=date_to,
                    metric="ORDERS",
                    question="",
                    nm_ids=nm_ids,
                )
                if isinstance(result, dict):
                    result["semantic_question"] = natural_question
                    result["semantic_resolution"] = resolution
                    result["normalized_query"] = deepcopy(resolution.get("normalized_query"))
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
            result["semantic_question"] = natural_question
            result["semantic_resolution"] = resolution
            result["normalized_query"] = deepcopy(resolution.get("normalized_query"))
            return result

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
    """Register the canonical server-side business-query entry point."""

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
