"""Compatibility data-catalog views backed exclusively by Semantic Core.

Historically the live infra carried a separate hard-coded data catalog. That
created a second place where dataset/metric routing could drift. These helpers
preserve the public MCP tool names while deriving every answer from the
canonical Semantic Core registries and Source Router.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import FastMCP

from .request_source_router import plan_marketplace_request
from .semantic_core import SEMANTIC_CORE_VERSION, semantic_core_view
from .semantic_resolver import resolve_semantic_question

DATA_CATALOG_VERSION = "marketplace_data_catalog.semantic_core_view.v1"


def build_data_catalog() -> dict[str, Any]:
    registry = semantic_core_view("registry")
    planning = semantic_core_view("planning")
    gaps = semantic_core_view("gaps")
    return {
        "version": DATA_CATALOG_VERSION,
        "status": "SEMANTIC_CORE_VIEW",
        "source_of_truth": "marketplace_semantic_core",
        "semantic_core_version": SEMANTIC_CORE_VERSION,
        "policy": {
            "independent_metric_routes_forbidden": True,
            "route_through_semantic_core": True,
            "silent_dataset_substitution_forbidden": True,
            "coverage_gate_required": True,
        },
        "sources": deepcopy(registry.get("sources") or {}),
        "datasets": deepcopy(registry.get("datasets") or {}),
        "capabilities": deepcopy(registry.get("capabilities") or {}),
        "availability_facts": deepcopy(planning.get("availability_facts") or {}),
        "known_gaps": gaps,
    }


def _marketplaces(value: str) -> tuple[str, ...]:
    requested = str(value or "all").strip().lower()
    if requested in {"wb", "wildberries", "вайлдберриз"}:
        return ("wb",)
    if requested in {"ozon", "озон"}:
        return ("ozon",)
    if requested in {"all", "both", "оба", ""}:
        return ("wb", "ozon")
    raise ValueError(f"unknown marketplace {value!r}")


def route_business_metric(metric_text: str, marketplace: str = "all") -> dict[str, Any]:
    """Resolve and plan a metric using Semantic Core, never a parallel alias map."""
    text = str(metric_text or "").strip()
    if not text:
        return {
            "ok": False,
            "status": "UNKNOWN",
            "reason": "empty metric",
            "source_of_truth": "marketplace_semantic_core",
        }

    resolution = resolve_semantic_question(text)
    if resolution.get("resolution_type") not in {"BUSINESS_METRIC", "CAPABILITY"}:
        return {
            "ok": False,
            "status": resolution.get("status") or "UNRESOLVED",
            "reason": resolution.get("reason") or resolution.get("guardrail") or (
                "Semantic Core did not resolve this text to an executable business metric/capability."
            ),
            "semantic_resolution": resolution,
            "source_of_truth": "marketplace_semantic_core",
        }

    try:
        marketplaces = _marketplaces(marketplace)
    except ValueError as exc:
        return {
            "ok": False,
            "status": "UNKNOWN_MARKETPLACE",
            "reason": str(exc),
            "marketplace": marketplace,
            "semantic_resolution": resolution,
            "source_of_truth": "marketplace_semantic_core",
        }

    plans = {
        service: plan_marketplace_request(text, marketplace=service)
        for service in marketplaces
    }
    return {
        "ok": True,
        "status": "RESOLVED",
        "source_of_truth": "marketplace_semantic_core",
        "semantic_resolution": resolution,
        "plans": plans,
        "rule": (
            "Source Router output is authoritative. A missing/unavailable source is a gap and must not be replaced by another dataset."
        ),
    }


def register_data_catalog_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="marketplace_data_catalog",
        annotations={
            "title": "Marketplace data catalog (Semantic Core view)",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_data_catalog() -> str:
        """Return sources/datasets/capabilities as a read-only Semantic Core view."""
        return json.dumps(build_data_catalog(), ensure_ascii=False, indent=2, default=str)

    @mcp.tool(
        name="marketplace_metric_route",
        annotations={
            "title": "Route a business metric through Semantic Core",
            "readOnlyHint": True,
            "openWorldHint": False,
        },
    )
    async def marketplace_metric_route(metric: str, marketplace: str = "all") -> str:
        """Resolve metric meaning and source family using the canonical brain."""
        return json.dumps(
            route_business_metric(metric, marketplace),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
