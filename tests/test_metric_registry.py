from core.metric_registry import load_metric_registry, resolve_metric_terms
from core.semantic_core import build_semantic_core_snapshot, semantic_core_view
from core.semantic_resolver import resolve_semantic_question


def test_metric_registry_v1_is_canonical_semantic_dictionary_not_execution_authority():
    registry = load_metric_registry()

    assert registry["version"] == "marketplace_metric_registry.v1"
    assert registry["policy"]["metric_dictionary_grants_execution"] is False
    assert registry["policy"]["provider_field_is_not_business_name"] is True
    assert registry["policy"]["unknown_provider_mapping_must_not_be_inferred"] is True
    assert len(registry["metrics"]) == 38


def test_payout_term_change_fee_has_exact_wb_semantics():
    metric = load_metric_registry()["metrics"]["PAYOUT_TERM_CHANGE_FEE"]
    assert metric["canonical_name_ru"] == "Комиссия за услугу «Вывести сейчас»"
    assert metric["semantic_target"] == {"type": "CAPABILITY", "id": "payout_term_change_fee"}
    assert metric["provider_mappings"]["wb"]["fields"] == ["paymentSchedule", "sellerOperName"]
    assert metric["provider_mappings"]["ozon"]["status"] == "NOT_APPLICABLE"


def test_drr_has_russian_canonical_name_latin_alias_and_real_wb_fields():
    drr = load_metric_registry()["metrics"]["AD_DRR"]

    assert drr["canonical_name_ru"] == "Доля рекламных расходов"
    assert drr["abbreviation"] == "ДРР"
    assert "DRR" in drr["aliases_en"]
    assert "доля рекламных расходов" in drr["aliases_ru"]
    assert drr["provider_mappings"]["wb"]["fields"] == ["spend", "attributed_order_amount"]
    assert drr["provider_mappings"]["ozon"]["status"] == "NOT_MAPPED"
    assert drr["formula_ref"].endswith("/drr_order_pct")
    assert "общий ДРР бизнеса" in drr["guardrail"]


def test_ctr_has_human_name_acronym_and_provider_mapping():
    ctr = load_metric_registry()["metrics"]["AD_CTR"]

    assert ctr["canonical_name_ru"] == "Кликабельность рекламы"
    assert ctr["abbreviation"] == "CTR"
    assert "click-through rate" in ctr["aliases_en"]
    assert ctr["provider_mappings"]["wb"]["fields"] == ["clicks", "views"]
    assert ctr["provider_mappings"]["wb"]["provider_aliases"] == ["ctr"]


def test_latin_drr_resolves_via_metric_dictionary_to_same_advertising_capability():
    result = resolve_semantic_question("Покажи DRR за август")

    assert result["resolution_type"] == "CAPABILITY"
    assert result["capability_id"] == "advertising_performance"
    assert result["route_id"].startswith("metric_dictionary:")
    assert result["canonical_metric"]["metric_id"] == "AD_DRR"
    assert result["canonical_metric"]["canonical_name_ru"] == "Доля рекламных расходов"
    assert result["execution_allowed"] is False


def test_cyrillic_drr_and_plain_language_map_to_same_canonical_metric():
    cyrillic = resolve_semantic_question("Какой ДРР был за август?")
    plain = resolve_semantic_question("Какая доля рекламных расходов была за август?")

    assert cyrillic["canonical_metric"]["metric_id"] == "AD_DRR"
    assert plain["canonical_metric"]["metric_id"] == "AD_DRR"
    assert cyrillic["capability_id"] == plain["capability_id"] == "advertising_performance"


def test_plain_language_ctr_resolves_even_without_legacy_intent_alias():
    result = resolve_semantic_question("Покажи кликабельность рекламы за август")

    assert result["capability_id"] == "advertising_performance"
    assert result["canonical_metric"]["metric_id"] == "AD_CTR"
    assert result["canonical_metric"]["abbreviation"] == "CTR"


def test_longer_drr_phrase_suppresses_nested_ad_spend_alias():
    matches = resolve_metric_terms("Покажи долю расходов на рекламу за август")

    assert [item["metric_id"] for item in matches] == ["AD_DRR"]


def test_metric_dictionary_is_visible_from_canonical_semantic_core():
    brain = build_semantic_core_snapshot()
    metrics_view = semantic_core_view("metrics")

    assert brain["validated"] is True
    assert brain["summary"]["counts"]["metric_dictionary_entries"] == 38
    assert brain["component_versions"]["metric_registry"] == "marketplace_metric_registry.v1"
    assert metrics_view["metrics"]["AD_DRR"]["abbreviation"] == "ДРР"
    assert brain["summary"]["safety"]["metric_dictionary_is_semantic_only"] is True


def test_ozon_stock_fields_have_distinct_stable_metric_ids():
    registry = load_metric_registry()["metrics"]

    assert registry["CURRENT_STOCK"]["provider_mappings"]["ozon"]["fields"] == [
        "stocks.present",
        "stocks.reserved",
        "stocks.type",
        "sku",
        "warehouse_ids",
    ]
    assert registry["OZON_FBO_AVAILABLE_STOCK"]["provider_mappings"]["ozon"]["fields"] == [
        "stocks.present", "stocks.reserved", "stocks.type"
    ]
    assert registry["OZON_FBO_RESERVED_STOCK"]["provider_mappings"]["ozon"]["fields"] == [
        "stocks.reserved", "stocks.type"
    ]
    assert registry["OZON_FBS_AVAILABLE_STOCK"]["provider_mappings"]["ozon"]["fields"] == [
        "stocks.present", "stocks.reserved", "stocks.type"
    ]
    assert registry["OZON_FBS_RESERVED_STOCK"]["provider_mappings"]["ozon"]["fields"] == [
        "stocks.reserved", "stocks.type"
    ]

    assert [item["metric_id"] for item in resolve_metric_terms("остаток FBO Ozon")] == [
        "OZON_FBO_AVAILABLE_STOCK"
    ]
    assert [item["metric_id"] for item in resolve_metric_terms("резерв FBS Ozon")] == [
        "OZON_FBS_RESERVED_STOCK"
    ]


def test_ozon_price_fields_have_distinct_stable_metric_ids():
    registry = load_metric_registry()["metrics"]

    assert registry["CURRENT_SELLING_PRICE"]["provider_mappings"]["ozon"]["fields"] == [
        "price.marketing_seller_price",
        "price.currency_code",
    ]
    assert registry["OZON_BASE_PRICE"]["provider_mappings"]["ozon"]["fields"] == [
        "price.price",
        "price.currency_code",
    ]
    assert registry["OZON_OLD_PRICE"]["provider_mappings"]["ozon"]["fields"] == [
        "price.old_price",
        "price.currency_code",
    ]
    assert registry["OZON_MIN_PRICE"]["provider_mappings"]["ozon"]["fields"] == [
        "price.min_price",
        "price.currency_code",
    ]

    assert [item["metric_id"] for item in resolve_metric_terms("минимальная цена Ozon")] == [
        "OZON_MIN_PRICE"
    ]
    assert [item["metric_id"] for item in resolve_metric_terms("старая цена Ozon")] == [
        "OZON_OLD_PRICE"
    ]
    assert [item["metric_id"] for item in resolve_metric_terms("цена до акций Ozon")] == [
        "OZON_BASE_PRICE"
    ]


def test_wb_price_fields_have_distinct_stable_metric_ids():
    registry = load_metric_registry()["metrics"]

    assert registry["WB_SELLER_PRICE_BEFORE_DISCOUNT"]["provider_mappings"]["wb"]["fields"] == ["price"]
    assert registry["WB_SELLER_PRICE_AFTER_DISCOUNT"]["provider_mappings"]["wb"]["fields"] == ["discountedPrice"]
    assert registry["WB_CLUB_PRICE_AFTER_DISCOUNT"]["provider_mappings"]["wb"]["fields"] == ["clubDiscountedPrice"]
