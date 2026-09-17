from core.semantic_resolver import resolve_semantic_question


def test_metric_dictionary_recognition_does_not_enable_capability_execution():
    result = resolve_semantic_question("Покажи DRR за август")

    assert result["canonical_metric"]["metric_id"] == "AD_DRR"
    assert result["resolution_type"] == "CAPABILITY"
    assert result["execution_allowed"] is False
    assert result["next_action"] == "BUILD_QUERY_PLAN_AFTER_COVERAGE_CHECK"
