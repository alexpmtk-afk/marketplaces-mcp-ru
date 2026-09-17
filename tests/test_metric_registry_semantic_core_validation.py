from copy import deepcopy

import pytest

from core.semantic_core import SemanticCoreError, build_semantic_core_snapshot, validate_semantic_core


def test_metric_dictionary_targets_must_exist_in_semantic_owners():
    brain = build_semantic_core_snapshot()
    broken = deepcopy(brain)
    broken["metric_dictionary"]["metrics"]["AD_DRR"]["semantic_target"] = {
        "type": "CAPABILITY",
        "id": "missing_capability",
    }

    with pytest.raises(SemanticCoreError, match="references unknown capability"):
        validate_semantic_core(broken)
