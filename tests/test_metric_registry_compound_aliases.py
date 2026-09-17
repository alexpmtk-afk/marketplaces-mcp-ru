from core.metric_registry import resolve_metric_terms


def test_multiple_independent_metric_terms_are_preserved():
    matches = resolve_metric_terms("Сравни показы и клики за август")

    assert {item["metric_id"] for item in matches} == {"AD_VIEWS", "AD_CLICKS"}
    assert {item["semantic_target"]["id"] for item in matches} == {"advertising_performance"}
