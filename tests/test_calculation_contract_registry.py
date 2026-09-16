from __future__ import annotations

from copy import deepcopy

import pytest

from core.calculation_contract_registry import (
    CALCULATION_REGISTRY,
    CONTRACT_VERSION,
    REGISTRY_VERSION,
    CalculationContractRegistryError,
    calculation_registry_summary,
    get_calculation_contract,
    registered_calculation_ids,
    validate_calculation_registry,
)


def _safe_ratio_contract() -> dict:
    return {
        "contract_id": "example_safe_ratio.v1",
        "contract_version": CONTRACT_VERSION,
        "status": "APPROVED",
        "business_meaning": "Example only: a pre-approved percentage between two aligned measures.",
        "marketplace": "wb",
        "operation": "RATIO_PERCENT",
        "required_scope_alignment": ["marketplace", "seller", "date_from", "date_to"],
        "currency_policy": "SAME_CURRENCY_REQUIRED",
        "zero_denominator_behavior": "BLOCK",
        "inputs": [
            {
                "alias": "numerator",
                "semantic_target": {"type": "CAPABILITY", "id": "advertising_performance"},
                "value_path": "metrics.spend",
                "allowed_source_families": ["CANONICAL_ARCHIVE"],
                "required_coverage": "FULL_COVERAGE",
                "data_class": "ADVERTISING_ATTRIBUTION_OPERATIONAL",
            },
            {
                "alias": "denominator",
                "semantic_target": {"type": "CAPABILITY", "id": "sale_and_return_operations"},
                "value_path": "calculation.sales_and_returns_by_currency[].net_sales_amount",
                "allowed_source_families": ["CANONICAL_ARCHIVE"],
                "required_coverage": "FULL_COVERAGE",
                "data_class": "REALIZED_SALES_AND_RETURNS",
            },
        ],
        "formula": {
            "operation": "RATIO_PERCENT",
            "numerator_input": "numerator",
            "denominator_input": "denominator",
        },
        "output": {
            "metric_id": "EXAMPLE_SAFE_RATIO",
            "unit": "PERCENT",
            "data_class": "DERIVED_EXAMPLE_ONLY",
        },
        "provenance_required": True,
    }


def _registry_with(contract: dict) -> dict:
    registry = deepcopy(CALCULATION_REGISTRY)
    registry["contracts"] = {contract["contract_id"]: contract}
    return registry


def test_default_registry_is_valid_but_intentionally_empty():
    validate_calculation_registry()
    summary = calculation_registry_summary()
    assert summary["registry_version"] == REGISTRY_VERSION
    assert summary["contract_version"] == CONTRACT_VERSION
    assert summary["registered_calculation_ids"] == []
    assert summary["registered_calculation_count"] == 0
    assert summary["arithmetic_enabled"] is False
    assert registered_calculation_ids() == []
    assert get_calculation_contract("anything") is None


def test_registry_schema_can_validate_a_fully_explicit_ratio_without_enabling_it():
    contract = _safe_ratio_contract()
    validate_calculation_registry(_registry_with(contract))
    # Validation of a hypothetical registry is not runtime registration.
    assert registered_calculation_ids() == []


def test_contract_requires_explicit_business_meaning():
    contract = _safe_ratio_contract()
    contract["business_meaning"] = ""
    with pytest.raises(CalculationContractRegistryError, match="business_meaning"):
        validate_calculation_registry(_registry_with(contract))


def test_contract_forbids_implicit_or_ad_hoc_currency_conversion():
    contract = _safe_ratio_contract()
    contract["currency_policy"] = "CONVERT_USD_TO_RUB_AUTOMATICALLY"
    with pytest.raises(CalculationContractRegistryError, match="currency policy"):
        validate_calculation_registry(_registry_with(contract))


def test_ratio_requires_explicit_zero_denominator_behavior():
    contract = _safe_ratio_contract()
    contract.pop("zero_denominator_behavior")
    with pytest.raises(CalculationContractRegistryError, match="zero_denominator_behavior"):
        validate_calculation_registry(_registry_with(contract))


def test_formula_may_reference_only_registered_inputs():
    contract = _safe_ratio_contract()
    contract["formula"]["denominator_input"] = "invented_sales"
    with pytest.raises(CalculationContractRegistryError, match="unregistered inputs"):
        validate_calculation_registry(_registry_with(contract))


def test_duplicate_or_unsafe_input_aliases_fail_closed():
    contract = _safe_ratio_contract()
    contract["inputs"][1]["alias"] = "numerator"
    with pytest.raises(CalculationContractRegistryError, match="duplicate input alias"):
        validate_calculation_registry(_registry_with(contract))

    contract = _safe_ratio_contract()
    contract["inputs"][0]["alias"] = "numerator.value"
    with pytest.raises(CalculationContractRegistryError, match="unsafe input alias"):
        validate_calculation_registry(_registry_with(contract))


def test_each_input_requires_known_coverage_and_specific_data_class():
    contract = _safe_ratio_contract()
    contract["inputs"][0]["required_coverage"] = "NONE"
    with pytest.raises(CalculationContractRegistryError, match="unsupported coverage requirement"):
        validate_calculation_registry(_registry_with(contract))

    contract = _safe_ratio_contract()
    contract["inputs"][0]["required_coverage"] = "TRUST_ME_COMPLETE"
    with pytest.raises(CalculationContractRegistryError, match="unsupported coverage requirement"):
        validate_calculation_registry(_registry_with(contract))

    contract = _safe_ratio_contract()
    contract["inputs"][0]["data_class"] = "ANY"
    with pytest.raises(CalculationContractRegistryError, match="unconstrained"):
        validate_calculation_registry(_registry_with(contract))


def test_output_data_class_cannot_be_unconstrained():
    contract = _safe_ratio_contract()
    contract["output"]["data_class"] = "ANY"
    with pytest.raises(CalculationContractRegistryError, match="output.data_class"):
        validate_calculation_registry(_registry_with(contract))


def test_scope_alignment_cannot_drop_seller_or_period():
    contract = _safe_ratio_contract()
    contract["required_scope_alignment"] = ["marketplace", "date_from", "date_to"]
    with pytest.raises(CalculationContractRegistryError, match="must align"):
        validate_calculation_registry(_registry_with(contract))
