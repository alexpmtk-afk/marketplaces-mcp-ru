from copy import deepcopy

import pytest

from core.semantic_registry import (
    SemanticRegistryError,
    get_dataset,
    load_semantic_registry,
    require_approved_binding,
    validate_semantic_registry,
)


def test_approved_metrics_use_approved_sources():
    registry = load_semantic_registry()
    for metric in registry["metrics"].values():
        if metric["status"] == "APPROVED":
            assert registry["sources"][metric["source_id"]]["status"] == "APPROVED"


def test_weekly_finance_schema_and_dedup_are_observed():
    dataset = get_dataset("wb_weekly_finance_main")
    assert dataset["field_count"] == 92
    assert len(dataset["fields"]) == 92
    assert dataset["row_dedup_key"] == ["reportId", "rrdId"]
    assert dataset["coverage"]["registry_file"] == "reports_registry.csv"
    assert dataset["coverage"]["archive_route_requirement"] == "FULL_COVERAGE"


def test_weekly_finance_business_bindings_fail_closed():
    registry = load_semantic_registry()
    for metric_id in (
        "ORDERS_UNITS",
        "ORDERS_AMOUNT_RUB",
        "SALES_NET_UNITS",
        "SALES_NET_AMOUNT_RUB",
    ):
        with pytest.raises(SemanticRegistryError):
            require_approved_binding(metric_id, "wb_weekly_finance_main", registry)


def test_validator_rejects_unapproved_executable_binding():
    registry = load_semantic_registry()
    unsafe = deepcopy(registry)
    binding = unsafe["bindings"]["wb_weekly_finance_main__SALES_NET_AMOUNT_RUB"]
    binding["execution_allowed"] = True
    with pytest.raises(SemanticRegistryError):
        validate_semantic_registry(unsafe)
