from __future__ import annotations

from core.data_catalog import build_data_catalog, route_business_metric
from core.semantic_core import semantic_core_view


def test_data_catalog_is_a_semantic_core_view_not_an_independent_registry() -> None:
    catalog = build_data_catalog()
    registry = semantic_core_view("registry")
    planning = semantic_core_view("planning")

    assert catalog["status"] == "SEMANTIC_CORE_VIEW"
    assert catalog["source_of_truth"] == "marketplace_semantic_core"
    assert catalog["policy"]["independent_metric_routes_forbidden"] is True
    assert catalog["sources"] == registry["sources"]
    assert catalog["datasets"] == registry["datasets"]
    assert catalog["capabilities"] == registry["capabilities"]
    assert catalog["availability_facts"] == planning["availability_facts"]


def test_metric_route_uses_semantic_resolver_and_source_router() -> None:
    result = route_business_metric("рекламные расходы", "wb")

    assert result["ok"] is True
    assert result["source_of_truth"] == "marketplace_semantic_core"
    assert result["semantic_resolution"]["resolution_type"] == "CAPABILITY"
    assert result["semantic_resolution"]["capability_id"] == "advertising_performance"
    assert result["plans"]["wb"]["source_family"] == "CANONICAL_ARCHIVE"


def test_metric_route_surfaces_unknown_meaning_instead_of_guessing() -> None:
    result = route_business_metric("совершенно неизвестная бизнес-метрика", "wb")

    assert result["ok"] is False
    assert result["source_of_truth"] == "marketplace_semantic_core"
