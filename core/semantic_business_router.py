"""Semantic business-query entry point with domain-specific archive executors."""
from __future__ import annotations

import json
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
    """Dispatch registered domain capabilities before the legacy finance path."""
    marketplace_key = str(marketplace or "").strip().lower()
    natural_question = str(question or "").strip()
    if natural_question and marketplace_key in {"wb", "wildberries"}:
        resolution = resolve_semantic_question(natural_question)
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
