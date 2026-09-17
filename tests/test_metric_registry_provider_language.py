from core.metric_registry import load_metric_registry


def test_human_and_provider_names_remain_separate():
    registry = load_metric_registry()
    spend = registry["metrics"]["AD_SPEND"]

    assert spend["canonical_name_ru"] == "Рекламные расходы"
    assert spend["provider_mappings"]["wb"]["fields"] == ["spend"]
    assert "spend" in spend["aliases_en"]
    assert registry["policy"]["provider_field_is_not_business_name"] is True
