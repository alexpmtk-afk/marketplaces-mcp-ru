from core.semantic_resolver import resolve_semantic_question


def test_sales_and_returns_have_separate_canonical_metric_meanings_same_capability():
    sales = resolve_semantic_question("Покажи продажи за август")
    returns = resolve_semantic_question("Покажи возвраты за август")

    assert sales["canonical_metric"]["metric_id"] == "SALES"
    assert returns["canonical_metric"]["metric_id"] == "RETURNS"
    assert sales["capability_id"] == returns["capability_id"] == "sale_and_return_operations"
