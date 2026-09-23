from copy import deepcopy
from datetime import date

import pytest

from core.marketplace_knowledge import (
    MarketplaceKnowledgeError,
    load_marketplace_knowledge_catalog,
    validate_knowledge_against_metric_registry,
    verify_marketplace_knowledge,
)
from core.metric_registry import load_metric_registry


def test_wb_price_knowledge_is_verified_and_source_backed():
    catalog = load_marketplace_knowledge_catalog()

    assert catalog["version"] == "marketplace_knowledge_catalog.v1"
    assert set(catalog["metrics"]) == {
        "WB_SELLER_PRICE_BEFORE_DISCOUNT",
        "WB_SELLER_PRICE_AFTER_DISCOUNT",
        "WB_CLUB_PRICE_AFTER_DISCOUNT",
    }
    for metric in catalog["metrics"].values():
        assert metric["semantic_status"] == "verified"
        assert metric["source_refs"]
        assert all(catalog["sources"][source_id]["official"] is True for source_id in metric["source_refs"])


def test_knowledge_catalog_cross_validates_against_metric_registry():
    catalog = load_marketplace_knowledge_catalog()
    registry = load_metric_registry()

    validate_knowledge_against_metric_registry(catalog, registry)


def test_knowledge_catalog_detects_provider_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["WB_SELLER_PRICE_AFTER_DISCOUNT"]["provider_mappings"]["wb"]["fields"] = ["changedField"]

    with pytest.raises(MarketplaceKnowledgeError, match="field mismatch"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_knowledge_verify_reports_fresh_pass_for_pilot_snapshot():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "PASS"
    assert result["verified_metric_count"] == 3
    assert result["stale_sources"] == []


def test_knowledge_verify_marks_old_sources_for_review_without_auto_update():
    result = verify_marketplace_knowledge(today=date(2026, 11, 1), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "STALE_REVIEW_REQUIRED"
    assert result["stale_sources"]
    assert result["production_auto_update"] is False
