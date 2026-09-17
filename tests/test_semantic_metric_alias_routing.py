from core.semantic_resolver import resolve_semantic_question


def test_english_acronyms_and_russian_names_converge_on_same_metric():
    pairs = [
        ("CTR", "кликабельность рекламы", "AD_CTR"),
        ("CPC", "стоимость клика", "AD_CPC"),
        ("CPO", "стоимость рекламного заказа", "AD_CPO"),
        ("DRR", "доля рекламных расходов", "AD_DRR"),
        ("ROAS", "окупаемость рекламных расходов", "AD_ROAS"),
    ]

    for acronym, russian_name, metric_id in pairs:
        english = resolve_semantic_question(f"Покажи {acronym} за август")
        russian = resolve_semantic_question(f"Покажи {russian_name} за август")

        assert english["canonical_metric"]["metric_id"] == metric_id
        assert russian["canonical_metric"]["metric_id"] == metric_id
        assert english["capability_id"] == russian["capability_id"] == "advertising_performance"


def test_provider_field_names_can_be_recognized_without_becoming_human_names():
    result = resolve_semantic_question("Покажи ad spend за август")

    assert result["canonical_metric"]["metric_id"] == "AD_SPEND"
    assert result["canonical_metric"]["canonical_name_ru"] == "Рекламные расходы"
    assert result["canonical_metric"]["provider_mappings"]["wb"]["fields"] == ["spend"]
