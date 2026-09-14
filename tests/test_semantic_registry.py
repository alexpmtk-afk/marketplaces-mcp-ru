from copy import deepcopy

import pytest

from core.semantic_registry import (
    SemanticRegistryError,
    get_capability,
    get_dataset,
    get_field,
    load_semantic_registry,
    require_available_capability,
    validate_semantic_registry,
)


def test_weekly_finance_has_semantics_for_every_physical_field():
    dataset = get_dataset("wb_weekly_finance_main")
    assert dataset["field_count"] == 92
    assert len(dataset["fields"]) == 92
    assert len(dataset["field_catalog"]) == 92
    assert set(dataset["fields"]) == set(dataset["field_catalog"])
    assert dataset["row_dedup_key"] == ["reportId", "rrdId"]
    assert dataset["coverage"]["archive_route_requirement"] == "FULL_COVERAGE"


def test_delivery_method_answers_only_observed_historical_fulfillment():
    capability = get_capability("observed_fulfillment_method")
    assert "deliveryMethod" in capability["fields"]
    assert capability["status"] == "AVAILABLE_WITH_LIMITATION"
    assert "current" in capability["guardrail"].lower()

    field = get_field("wb_weekly_finance_main", "deliveryMethod")
    assert field["role"] == "dimension"
    assert any("истор" in item.lower() for item in field["limitations"])


def test_order_date_cannot_be_used_as_complete_orders_metric():
    field = get_field("wb_weekly_finance_main", "orderDt")
    assert any("полного потока заказов" in item for item in field["limitations"])

    registry = load_semantic_registry()
    missing = registry["not_covered"]["all_orders_placed"]
    assert missing["required_source_id"] == "wb_order_feed"
    assert registry["sources"]["wb_order_feed"]["database_presence"] == "NOT_IN_DATABASE"

    with pytest.raises(SemanticRegistryError):
        require_available_capability("all_orders_placed", registry)


def test_penalty_semantics_include_amount_and_reason():
    capability = require_available_capability("penalties")
    assert "penalty" in capability["fields"]
    assert "bonusTypeName" in capability["fields"]
    assert "sellerOperName" in capability["fields"]


def test_weekly_report_can_answer_charge_but_not_detailed_storage_driver():
    storage = require_available_capability("storage_charge")
    assert "paidStorage" in storage["fields"]
    assert storage["status"] == "AVAILABLE_WITH_LIMITATION"

    registry = load_semantic_registry()
    detailed = registry["not_covered"]["detailed_storage_calculation"]
    assert detailed["required_source_id"] == "wb_paid_storage_report"
    assert (
        registry["sources"]["wb_paid_storage_report"]["database_presence"]
        == "NOT_IN_DATABASE"
    )


def test_other_wb_and_ozon_sources_are_reference_only_not_database_presence():
    registry = load_semantic_registry()
    for source_id in (
        "wb_order_feed",
        "wb_paid_storage_report",
        "wb_acceptance_operations_report",
        "wb_stock_report",
        "wb_ads_data",
        "ozon_reports",
    ):
        source = registry["sources"][source_id]
        assert source["database_presence"] == "NOT_IN_DATABASE"
        assert source["execution_status"] == "REFERENCE_ONLY"


def test_validator_rejects_available_capability_backed_by_missing_source():
    registry = load_semantic_registry()
    unsafe = deepcopy(registry)
    unsafe["capabilities"]["penalties"]["source_id"] = "wb_ads_data"
    with pytest.raises(SemanticRegistryError):
        validate_semantic_registry(unsafe)


def test_validator_rejects_missing_field_semantics():
    registry = load_semantic_registry()
    unsafe = deepcopy(registry)
    del unsafe["datasets"]["wb_weekly_finance_main"]["field_catalog"]["penalty"]
    with pytest.raises(SemanticRegistryError):
        validate_semantic_registry(unsafe)
