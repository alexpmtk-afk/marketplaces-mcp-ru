from copy import deepcopy
from datetime import date

import pytest

from core.marketplace_knowledge import (
    MarketplaceKnowledgeError,
    get_knowledge_metric,
    load_marketplace_knowledge_catalog,
    validate_knowledge_against_metric_registry,
    verify_marketplace_knowledge,
)
from core.metric_registry import load_metric_registry


def test_wb_price_knowledge_remains_verified_and_source_backed():
    catalog = load_marketplace_knowledge_catalog()
    price_ids = {
        "WB_SELLER_PRICE_BEFORE_DISCOUNT",
        "WB_SELLER_PRICE_AFTER_DISCOUNT",
        "WB_CLUB_PRICE_AFTER_DISCOUNT",
    }

    assert catalog["version"] == "marketplace_knowledge_catalog.v1"
    assert price_ids <= set(catalog["metrics"])
    for metric_id in price_ids:
        metric = catalog["metrics"][metric_id]
        assert metric["semantic_status"] == "verified"
        assert metric["source_refs"]
        assert all(
            catalog["sources"][source_id]["official"] is True
            for source_id in metric["source_refs"]
        )


def test_provider_specific_stock_knowledge_does_not_cross_marketplaces():
    wb = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="wb", source_field="quantity"
    )
    ozon = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="ozon", source_field="stocks.present"
    )

    assert wb is not None
    assert wb["knowledge_id"] == "WB_CURRENT_STOCK"
    assert wb["label_ru"] == "Текущие остатки на складах WB"
    assert wb["semantic_status"] == "verified"

    assert ozon is not None
    assert ozon["knowledge_id"] == "OZON_CURRENT_STOCK"
    assert ozon["semantic_status"] == "provisional"

    assert get_knowledge_metric(
        "CURRENT_STOCK", marketplace="ozon", source_field="quantity"
    ) is None
    assert get_knowledge_metric("CURRENT_STOCK") is None


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


def test_wb_stock_knowledge_detects_quantity_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["CURRENT_STOCK"]["provider_mappings"]["wb"]["fields"] = [
        "warehouseName"
    ]

    with pytest.raises(MarketplaceKnowledgeError, match="WB_CURRENT_STOCK"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_knowledge_verify_reports_verified_and_provisional_bindings():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "PASS"
    assert result["knowledge_record_count"] == 6
    assert result["semantic_metric_count"] == 5
    assert result["verified_metric_count"] == 4
    assert result["verified_binding_count"] == 4
    assert result["provisional_binding_count"] == 2
    assert {
        (item["metric_id"], item["marketplace"])
        for item in result["provisional_bindings"]
    } == {
        ("CURRENT_STOCK", "ozon"),
        ("CURRENT_SELLING_PRICE", "ozon"),
    }
    assert result["stale_sources"] == []


def test_knowledge_verify_marks_old_sources_for_review_without_auto_update():
    result = verify_marketplace_knowledge(today=date(2026, 11, 1), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "STALE_REVIEW_REQUIRED"
    assert result["stale_sources"]
    assert result["production_auto_update"] is False
