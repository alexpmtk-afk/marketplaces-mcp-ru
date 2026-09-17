from copy import deepcopy

import pytest

from core.metric_registry import MetricRegistryError, load_metric_registry, validate_metric_registry


def test_ozon_mappings_are_explicitly_fail_closed_for_cross_marketplace_metrics():
    registry = load_metric_registry()

    assert registry["policy"]["ozon_unmapped_must_fail_closed"] is True
    assert registry["metrics"]["AD_DRR"]["provider_mappings"]["ozon"]["status"] == "NOT_MAPPED"
    assert registry["metrics"]["AD_CTR"]["provider_mappings"]["ozon"]["status"] == "NOT_MAPPED"
    assert registry["metrics"]["SALES"]["provider_mappings"]["ozon"]["status"] == "NOT_MAPPED"


def test_derived_metric_without_formula_reference_is_rejected():
    registry = load_metric_registry()
    broken = deepcopy(registry)
    broken["metrics"]["AD_DRR"].pop("formula_ref")

    with pytest.raises(MetricRegistryError, match="must define formula_ref"):
        validate_metric_registry(broken)


def test_dictionary_cannot_be_promoted_to_execution_authority():
    registry = load_metric_registry()
    broken = deepcopy(registry)
    broken["policy"]["metric_dictionary_grants_execution"] = True

    with pytest.raises(MetricRegistryError, match="must not grant execution permission"):
        validate_metric_registry(broken)
