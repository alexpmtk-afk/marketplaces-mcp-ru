from copy import deepcopy

import pytest

from core.metric_registry import MetricRegistryError, load_metric_registry, validate_metric_registry


def test_alias_cannot_belong_to_two_different_canonical_metrics():
    registry = load_metric_registry()
    broken = deepcopy(registry)
    broken["metrics"]["AD_CPC"]["aliases_en"].append("CTR")

    with pytest.raises(MetricRegistryError, match="shared by metrics"):
        validate_metric_registry(broken)
