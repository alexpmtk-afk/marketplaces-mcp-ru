from copy import deepcopy
from datetime import date

import pytest

from core.marketplace_knowledge import (
    MarketplaceKnowledgeError,
    get_knowledge_metric,
    get_report_field_knowledge,
    load_marketplace_knowledge_catalog,
    validate_knowledge_against_metric_registry,
    verify_marketplace_knowledge,
)
from core.metric_registry import load_metric_registry


def test_wb_price_knowledge_remains_verified_and_source_backed():
    catalog = load_marketplace_knowledge_catalog()
    price_ids = {
        "WB_SELLER_PRICE_BEFORE_DISCOUNT",
        "WB_SELLER_PRICE_AFTER_DISCOUNT",
        "WB_CLUB_PRICE_AFTER_DISCOUNT",
    }

    assert catalog["version"] == "marketplace_knowledge_catalog.v1"
    assert price_ids <= set(catalog["metrics"])
    for metric_id in price_ids:
        metric = catalog["metrics"][metric_id]
        assert metric["semantic_status"] == "verified"
        assert metric["source_refs"]
        assert all(
            catalog["sources"][source_id]["official"] is True
            for source_id in metric["source_refs"]
        )


def test_wb_price_cabinet_bindings_use_exact_seller_labels():
    before = get_knowledge_metric(
        "WB_SELLER_PRICE_BEFORE_DISCOUNT", marketplace="wb", source_field="price"
    )
    discounted = get_knowledge_metric(
        "WB_SELLER_PRICE_AFTER_DISCOUNT", marketplace="wb", source_field="discountedPrice"
    )
    club = get_knowledge_metric(
        "WB_CLUB_PRICE_AFTER_DISCOUNT", marketplace="wb", source_field="clubDiscountedPrice"
    )

    assert before["cabinet_binding"]["surface_ru"] == "Товары и цены → Цены и скидки"
    assert before["cabinet_binding"]["label_ru"] == "Цена продавца до скидки, ₽"
    assert discounted["cabinet_binding"]["label_ru"] == "Цена со скидкой, ₽"
    assert club["cabinet_binding"]["label_ru"] == "Цена со скидкой для WB Клуба, ₽"
    for metric in (before, discounted, club):
        assert metric["cabinet_binding"]["equivalence_status"] == "verified"
        assert metric["cabinet_binding"]["source_refs"] == ["wb_seller_price_help_20260518"]


def test_ozon_price_knowledge_keeps_each_provider_field_separate():
    current = get_knowledge_metric(
        "CURRENT_SELLING_PRICE", marketplace="ozon", source_field="price.marketing_seller_price"
    )
    base = get_knowledge_metric(
        "OZON_BASE_PRICE", marketplace="ozon", source_field="price.price"
    )
    old = get_knowledge_metric(
        "OZON_OLD_PRICE", marketplace="ozon", source_field="price.old_price"
    )
    minimum = get_knowledge_metric(
        "OZON_MIN_PRICE", marketplace="ozon", source_field="price.min_price"
    )

    assert current["label_ru"] == "Цена Ozon с учётом акций продавца"
    assert base["label_ru"] == "Цена продавца до акций Ozon"
    assert old["label_ru"] == "Зачёркнутая цена Ozon"
    assert minimum["label_ru"] == "Минимально допустимая цена Ozon"

    assert {
        current["provider_binding"]["field_path"],
        base["provider_binding"]["field_path"],
        old["provider_binding"]["field_path"],
        minimum["provider_binding"]["field_path"],
    } == {
        "price.marketing_seller_price",
        "price.price",
        "price.old_price",
        "price.min_price",
    }
    assert all(
        metric["semantic_status"] == "provisional"
        for metric in (current, base, old, minimum)
    )


def test_ozon_stock_knowledge_uses_present_minus_reserved_and_keeps_fbo_fbs_separate():
    total = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="ozon", source_field="stocks.present"
    )
    fbo_available = get_knowledge_metric(
        "OZON_FBO_AVAILABLE_STOCK", marketplace="ozon", source_field="stocks.present"
    )
    fbo_reserved = get_knowledge_metric(
        "OZON_FBO_RESERVED_STOCK", marketplace="ozon", source_field="stocks.reserved"
    )
    fbs_available = get_knowledge_metric(
        "OZON_FBS_AVAILABLE_STOCK", marketplace="ozon", source_field="stocks.present"
    )
    fbs_reserved = get_knowledge_metric(
        "OZON_FBS_RESERVED_STOCK", marketplace="ozon", source_field="stocks.reserved"
    )

    assert total["label_ru"] == "Доступный остаток Ozon"
    assert total["provider_binding"]["field_path"] == "stocks.present"
    assert total["calculation_inputs"] == [
        "stocks.present", "stocks.reserved", "stocks.type"
    ]
    assert "present - reserved" in total["definition_ru"]

    assert fbo_available["label_ru"] == "Доступный остаток FBO Ozon"
    assert fbo_reserved["label_ru"] == "Резерв FBO Ozon"
    assert fbs_available["label_ru"] == "Доступный остаток FBS Ozon"
    assert fbs_reserved["label_ru"] == "Резерв FBS Ozon"

    assert all(
        item["semantic_status"] == "provisional"
        for item in (total, fbo_available, fbo_reserved, fbs_available, fbs_reserved)
    )


def test_ozon_order_and_posting_knowledge_are_distinct():
    orders = get_knowledge_metric(
        "OZON_ORDERS", marketplace="ozon", source_field="order_number"
    )
    postings = get_knowledge_metric(
        "OZON_POSTINGS", marketplace="ozon", source_field="posting_number"
    )

    assert orders is not None
    assert postings is not None
    assert orders["label_ru"] == "Заказы Ozon"
    assert postings["label_ru"] == "Отправления Ozon"
    assert orders["provider_binding"]["field_path"] == "order_number"
    assert postings["provider_binding"]["field_path"] == "posting_number"
    assert "FBO и FBS нельзя просто складывать" in orders["definition_ru"]
    assert "status и substatus" in postings["definition_ru"]
    assert orders["semantic_status"] == "provisional"
    assert postings["semantic_status"] == "provisional"


def test_provider_specific_stock_knowledge_does_not_cross_marketplaces():
    wb = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="wb", source_field="quantity"
    )
    ozon = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="ozon", source_field="stocks.present"
    )

    assert wb is not None
    assert wb["knowledge_id"] == "WB_CURRENT_STOCK"
    assert wb["label_ru"] == "Текущие остатки на складах WB"
    assert wb["semantic_status"] == "verified"

    assert ozon is not None
    assert ozon["knowledge_id"] == "OZON_CURRENT_STOCK"
    assert ozon["semantic_status"] == "provisional"

    assert get_knowledge_metric(
        "CURRENT_STOCK", marketplace="ozon", source_field="quantity"
    ) is None
    assert get_knowledge_metric("CURRENT_STOCK") is None


def test_wb_stock_cabinet_bindings_are_verified_and_keep_fbw_fbs_separate():
    wb_stock = get_knowledge_metric(
        "CURRENT_STOCK", marketplace="wb", source_field="quantity"
    )
    fbs_stock = get_knowledge_metric(
        "CURRENT_FBS_STOCK", marketplace="wb", source_field="amount"
    )

    assert wb_stock["cabinet_binding"] == {
        "surface_ru": "Товары и цены → карточка товара → Остатки",
        "label_ru": "Остатки «Склад WB»",
        "scope_ru": (
            "Товары, физически находящиеся на складах Wildberries. "
            "В карточке товара этот bucket показывается отдельно от "
            "«Свой склад» и от «Товары в пути»."
        ),
        "equivalence_status": "verified",
        "source_refs": ["wb_warehouse_stock_report_help_20260518"],
        "live_cabinet_observation": (
            "2026-09-24: seller cabinet product panel uses exact label "
            "«Остатки “Склад WB”»."
        ),
    }
    assert fbs_stock["cabinet_binding"] == {
        "surface_ru": "Товары и цены → карточка товара → Остатки",
        "label_ru": "Остатки «Свой склад»",
        "scope_ru": (
            "Суммарный текущий остаток на виртуальных складах продавца. "
            "Названия конкретных складов, например «АНТ ГК», являются детализацией "
            "внутри этого bucket, а не названием показателя."
        ),
        "equivalence_status": "verified",
        "source_refs": ["wb_fbs_stock_management_help_20260827"],
        "live_cabinet_observation": (
            "2026-09-24: seller cabinet product panel uses exact label "
            "«Остатки “Свой склад”»."
        ),
    }
    assert wb_stock["provider_binding"]["field_path"] == "quantity"
    assert fbs_stock["provider_binding"]["field_paths"] == ["chrtId", "amount"]


def test_verified_cabinet_binding_requires_official_source():
    catalog = load_marketplace_knowledge_catalog()
    catalog = deepcopy(catalog)
    catalog["sources"]["wb_warehouse_stock_report_help_20260518"]["official"] = False

    from core.marketplace_knowledge import validate_marketplace_knowledge_catalog

    with pytest.raises(MarketplaceKnowledgeError, match="non-official sources"):
        validate_marketplace_knowledge_catalog(catalog)


def test_knowledge_catalog_cross_validates_against_metric_registry():
    catalog = load_marketplace_knowledge_catalog()
    registry = load_metric_registry()

    validate_knowledge_against_metric_registry(catalog, registry)


def test_knowledge_catalog_detects_provider_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["WB_SELLER_PRICE_AFTER_DISCOUNT"]["provider_mappings"]["wb"]["fields"] = ["changedField"]

    with pytest.raises(MarketplaceKnowledgeError, match="field mismatch"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_wb_stock_knowledge_detects_quantity_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["CURRENT_STOCK"]["provider_mappings"]["wb"]["fields"] = [
        "warehouseName"
    ]

    with pytest.raises(MarketplaceKnowledgeError, match="WB_CURRENT_STOCK"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_knowledge_verify_reports_verified_and_provisional_bindings():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "PASS"
    assert result["knowledge_record_count"] == 43
    assert result["semantic_metric_count"] == 40
    assert result["verified_metric_count"] == 30
    assert result["verified_binding_count"] == 30
    assert result["provisional_binding_count"] == 13
    assert {
        (item["metric_id"], item["marketplace"])
        for item in result["provisional_bindings"]
    } == {
        ("CURRENT_STOCK", "ozon"),
        ("CURRENT_SELLING_PRICE", "ozon"),
        ("OZON_BASE_PRICE", "ozon"),
        ("OZON_OLD_PRICE", "ozon"),
        ("OZON_MIN_PRICE", "ozon"),
        ("OZON_FBO_AVAILABLE_STOCK", "ozon"),
        ("OZON_FBO_RESERVED_STOCK", "ozon"),
        ("OZON_FBS_AVAILABLE_STOCK", "ozon"),
        ("OZON_FBS_RESERVED_STOCK", "ozon"),
        ("OZON_ORDERS", "ozon"),
        ("OZON_POSTINGS", "ozon"),
        ("SALES", "ozon"),
        ("RETURNS", "ozon"),
    }
    assert result["stale_sources"] == []


def test_knowledge_verify_marks_old_sources_for_review_without_auto_update():
    result = verify_marketplace_knowledge(today=date(2026, 11, 1), max_source_age_days=30)

    assert result["ok"] is True
    assert result["status"] == "STALE_REVIEW_REQUIRED"
    assert result["stale_sources"]
    assert result["production_auto_update"] is False


def test_wb_cabinet_classification_is_complete_and_fail_closed():
    result = verify_marketplace_knowledge(today=date(2026, 9, 24), max_source_age_days=30)
    wb = result["cabinet_binding_coverage"]["wb"]

    assert wb["cabinet_classification_complete"] is True
    assert wb["missing_cabinet_classification_count"] == 0
    assert wb["missing_cabinet_classifications"] == []
    assert wb["no_confirmed_direct_equivalent_records"] == [
        "WB_AD_ADVERTISED_ITEMS",
        "WB_AD_ROAS",
    ]
    assert wb["classification_status_counts"]["none"] == 2
    assert wb["classified_record_count"] == wb["verified_knowledge_record_count"]


def test_knowledge_verify_reports_registry_coverage_gaps_by_marketplace():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    coverage = result["provider_metric_coverage"]

    wb = coverage["wb"]
    assert wb["registry_mapping_status_counts"]["NOT_MAPPED"] == 1
    assert wb["registry_unresolved_metric_ids"] == ["CURRENT_SELLING_PRICE"]
    assert "CURRENT_STOCK" in wb["verified_metric_ids"]
    assert set(wb["missing_knowledge_metric_ids"]) == set()
    assert wb["metric_coverage_complete"] is False

    ozon = coverage["ozon"]
    assert ozon["registry_mapping_status_counts"]["NOT_MAPPED"] == 22
    assert set(ozon["knowledge_bound_metric_ids"]) >= {
        "CURRENT_STOCK",
        "CURRENT_SELLING_PRICE",
    }
    assert set(ozon["provisional_metric_ids"]) == {
        "CURRENT_STOCK",
        "CURRENT_SELLING_PRICE",
        "OZON_BASE_PRICE",
        "OZON_OLD_PRICE",
        "OZON_MIN_PRICE",
        "OZON_FBO_AVAILABLE_STOCK",
        "OZON_FBO_RESERVED_STOCK",
        "OZON_FBS_AVAILABLE_STOCK",
        "OZON_FBS_RESERVED_STOCK",
        "OZON_ORDERS",
        "OZON_POSTINGS",
        "SALES",
        "RETURNS",
    }
    assert "ORDERS" in ozon["registry_unresolved_metric_ids"]
    assert ozon["metric_coverage_complete"] is False


def test_ozon_final_sales_returns_knowledge_uses_separate_unit_fields():
    sales = get_knowledge_metric(
        "SALES", marketplace="ozon", source_field="delivery_commission.quantity"
    )
    returns = get_knowledge_metric(
        "RETURNS", marketplace="ozon", source_field="return_commission.quantity"
    )

    assert sales is not None
    assert returns is not None
    assert sales["knowledge_id"] == "OZON_FINAL_SALES"
    assert returns["knowledge_id"] == "OZON_FINAL_RETURNS"
    assert sales["unit"] == "UNITS"
    assert returns["unit"] == "UNITS"
    assert sales["semantic_status"] == "provisional"
    assert returns["semantic_status"] == "provisional"
    assert "amount/total/standard_fee" in sales["guardrail"]
    assert "amount/total/standard_fee" in returns["guardrail"]


def test_composite_wb_finance_binding_resolves_each_canonical_field():
    for field in ("docTypeName", "saleDt", "quantity", "retailAmount"):
        sales = get_knowledge_metric("SALES", marketplace="wb", source_field=field)
        returns = get_knowledge_metric("RETURNS", marketplace="wb", source_field=field)
        assert sales is not None
        assert sales["knowledge_id"] == "WB_SALES"
        assert sales["semantic_status"] == "verified"
        assert returns is not None
        assert returns["knowledge_id"] == "WB_RETURNS"
        assert returns["semantic_status"] == "verified"


def test_wb_sales_and_returns_cabinet_bindings_are_partial_not_equal():
    sales = get_knowledge_metric("SALES", marketplace="wb", source_field="retailAmount")
    returns = get_knowledge_metric("RETURNS", marketplace="wb", source_field="retailAmount")

    assert sales is not None
    assert returns is not None
    assert sales["cabinet_binding"]["surface_ru"] == "Аналитика продавца → Лента заказов"
    assert sales["cabinet_binding"]["label_ru"] == "Выкупы"
    assert sales["cabinet_binding"]["equivalence_status"] == "partial"
    assert "docTypeName=Продажа" in sales["cabinet_binding"]["scope_ru"]
    assert "Не приравнивать" in sales["guardrail"]

    assert returns["cabinet_binding"]["surface_ru"] == "Аналитика продавца → Лента заказов"
    assert returns["cabinet_binding"]["label_ru"] == "Возвраты"
    assert returns["cabinet_binding"]["equivalence_status"] == "partial"
    assert "docTypeName=Возврат" in returns["cabinet_binding"]["scope_ru"]
    assert "Не приравнивать" in returns["guardrail"]


def test_composite_binding_detects_one_missing_registry_field():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["LOGISTICS_COST"]["provider_mappings"]["wb"]["fields"] = [
        "deliveryAmount",
        "returnAmount",
        "deliveryService",
    ]

    with pytest.raises(MarketplaceKnowledgeError, match="rebillLogisticCost"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_wb_finance_metrics_are_verified_knowledge():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    verified = set(result["provider_metric_coverage"]["wb"]["verified_metric_ids"])
    assert {
        "SALES",
        "RETURNS",
        "LOGISTICS_COST",
        "PENALTIES",
        "STORAGE_COST",
        "ACCEPTANCE_COST",
        "DEDUCTIONS_ADJUSTMENTS",
        "WB_COMMISSION_REWARD",
        "ACQUIRING_PAYMENT_PROCESSING",
        "PAYOUT_TERM_CHANGE_FEE",
    } <= verified


def test_wb_logistics_service_cabinet_bindings_use_weekly_report_labels():
    logistics = get_knowledge_metric(
        "LOGISTICS_COST", marketplace="wb", source_field="deliveryService"
    )
    storage = get_knowledge_metric(
        "STORAGE_COST", marketplace="wb", source_field="paidStorage"
    )
    acceptance = get_knowledge_metric(
        "ACCEPTANCE_COST", marketplace="wb", source_field="paidAcceptance"
    )
    penalties = get_knowledge_metric(
        "PENALTIES", marketplace="wb", source_field="penalty"
    )
    payout = get_knowledge_metric(
        "PAYOUT_TERM_CHANGE_FEE", marketplace="wb", source_field="paymentSchedule"
    )

    assert logistics["cabinet_binding"]["label_ru"] == "Стоимость логистики"
    assert storage["cabinet_binding"]["label_ru"] == "Стоимость хранения"
    assert acceptance["cabinet_binding"]["label_ru"] == "Стоимость операций при приёмке"
    assert penalties["cabinet_binding"]["label_ru"] == "Общая сумма штрафов"
    assert payout["cabinet_binding"]["label_ru"] == "Разовое изменение срока перечисления денежных средств"
    assert payout["cabinet_binding"]["surface_ru"] == "Финансовые отчёты → Еженедельные"
    assert payout["cabinet_binding"]["equivalence_status"] == "partial"
    assert payout["provider_binding"]["field_paths"] == ["paymentSchedule", "sellerOperName"]

    for metric in (logistics, storage, acceptance, penalties, payout):
        assert metric["cabinet_binding"]["surface_ru"] == "Финансовые отчёты → Еженедельные"
        assert metric["cabinet_binding"]["equivalence_status"] == "partial"
        assert "Не " in metric["guardrail"]


def test_wb_finance_cabinet_bindings_are_partial_and_period_safe():
    deductions = get_knowledge_metric(
        "DEDUCTIONS_ADJUSTMENTS", marketplace="wb", source_field="deduction"
    )
    commission = get_knowledge_metric(
        "WB_COMMISSION_REWARD", marketplace="wb", source_field="vw"
    )
    acquiring = get_knowledge_metric(
        "ACQUIRING_PAYMENT_PROCESSING", marketplace="wb", source_field="acquiringFee"
    )

    assert deductions["cabinet_binding"]["surface_ru"] == "Главная → Баланс → Приход и расход"
    assert deductions["cabinet_binding"]["label_ru"] == "Удержания / доплаты"
    assert deductions["cabinet_binding"]["equivalence_status"] == "partial"
    assert "оперативный слой" in deductions["cabinet_binding"]["scope_ru"]

    assert commission["cabinet_binding"]["surface_ru"] == "Главная → Баланс → Приход и расход"
    assert commission["cabinet_binding"]["label_ru"] == "Комиссия Wildberries"
    assert commission["cabinet_binding"]["equivalence_status"] == "partial"
    assert "не сворачиваются" in commission["cabinet_binding"]["scope_ru"]

    assert acquiring["cabinet_binding"]["surface_ru"] == (
        "Финансовые отчёты → Отчёт об издержках на приём платежей"
    )
    assert acquiring["cabinet_binding"]["label_ru"] == "Издержки на приём платежей"
    assert acquiring["cabinet_binding"]["equivalence_status"] == "partial"
    assert "месячный" in acquiring["cabinet_binding"]["scope_ru"]
    assert "Не приравнивать" in acquiring["guardrail"]


def test_wb_operational_orders_and_fbs_stock_are_verified():
    orders = get_knowledge_metric("ORDERS", marketplace="wb", source_field="finishedPrice")
    fbs = get_knowledge_metric("CURRENT_FBS_STOCK", marketplace="wb", source_field="amount")

    assert orders is not None
    assert orders["knowledge_id"] == "WB_ORDERS"
    assert orders["semantic_status"] == "verified"
    assert fbs is not None
    assert fbs["knowledge_id"] == "WB_CURRENT_FBS_STOCK"
    assert fbs["semantic_status"] == "verified"


def test_wb_advertising_cabinet_bindings_match_official_labels_and_formulas():
    expected = {
        ("AD_VIEWS", "views"): ("Показы", "verified"),
        ("AD_CLICKS", "clicks"): ("Клики", "verified"),
        ("AD_CART_ADDS", "cart_adds"): ("Добавление в корзину", "verified"),
        ("AD_ORDERS", "ad_orders"): ("Заказы", "verified"),
        ("AD_CANCELED", "canceled"): ("Отмены", "partial"),
        ("AD_SPEND", "spend"): ("Затраты, ₽", "verified"),
        ("AD_ATTRIBUTED_ORDER_AMOUNT", "attributed_order_amount"): ("Заказов на сумму", "verified"),
        ("AD_CTR", "clicks"): ("CTR, %", "verified"),
        ("AD_CPC", "spend"): ("CPC, ₽", "verified"),
        ("AD_CLICK_TO_ORDER_CR", "ad_orders"): ("CR, %", "verified"),
        ("AD_CPO", "spend"): ("CPO, ₽", "verified"),
        ("AD_DRR", "spend"): ("Доля затрат, %", "verified"),
    }
    for (metric_id, field), (label, status) in expected.items():
        metric = get_knowledge_metric(metric_id, marketplace="wb", source_field=field)
        assert metric is not None
        assert metric["cabinet_binding"]["label_ru"] == label
        assert metric["cabinet_binding"]["equivalence_status"] == status
        assert metric["cabinet_binding"]["source_refs"] == ["wb_promotion_stats_help_20260617"]

    ctr = get_knowledge_metric("AD_CTR", marketplace="wb", source_field="views")
    cpc = get_knowledge_metric("AD_CPC", marketplace="wb", source_field="clicks")
    cr = get_knowledge_metric("AD_CLICK_TO_ORDER_CR", marketplace="wb", source_field="clicks")
    cpo = get_knowledge_metric("AD_CPO", marketplace="wb", source_field="ad_orders")
    drr = get_knowledge_metric("AD_DRR", marketplace="wb", source_field="attributed_order_amount")
    assert "Клики ÷ Показы × 100" in ctr["cabinet_binding"]["scope_ru"]
    assert "Затраты ÷ Клики" in cpc["cabinet_binding"]["scope_ru"]
    assert "Заказы ÷ Клики × 100" in cr["cabinet_binding"]["scope_ru"]
    assert "Затраты ÷ Заказы" in cpo["cabinet_binding"]["scope_ru"]
    assert "Затраты ÷ сумма заказов" in drr["cabinet_binding"]["scope_ru"]


def test_wb_roas_and_advertised_items_are_explicitly_classified_without_fake_labels():
    roas = get_knowledge_metric("AD_ROAS", marketplace="wb", source_field="spend")
    advertised = get_knowledge_metric(
        "AD_ADVERTISED_ITEMS", marketplace="wb", source_field="advertised_items"
    )
    for metric in (roas, advertised):
        assert metric is not None
        assert metric["cabinet_binding"]["equivalence_status"] == "none"
        assert metric["cabinet_binding"]["label_ru"] == "Нет отдельного подтверждённого показателя"
        assert metric["cabinet_binding"]["source_refs"] == ["wb_promotion_stats_help_20260617"]


def test_wb_advertising_metric_family_is_fully_verified():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    verified = set(result["provider_metric_coverage"]["wb"]["verified_metric_ids"])
    assert {
        "AD_VIEWS",
        "AD_CLICKS",
        "AD_CART_ADDS",
        "AD_ORDERS",
        "AD_ADVERTISED_ITEMS",
        "AD_CANCELED",
        "AD_SPEND",
        "AD_ATTRIBUTED_ORDER_AMOUNT",
        "AD_CTR",
        "AD_CPC",
        "AD_CLICK_TO_ORDER_CR",
        "AD_CPO",
        "AD_DRR",
        "AD_ROAS",
    } <= verified


def test_wb_advertising_raw_and_derived_bindings_do_not_cross_metrics():
    assert get_knowledge_metric(
        "AD_CART_ADDS", marketplace="wb", source_field="cart_adds"
    )["knowledge_id"] == "WB_AD_CART_ADDS"
    assert get_knowledge_metric(
        "AD_CTR", marketplace="wb", source_field="views"
    )["knowledge_id"] == "WB_AD_CTR"
    assert get_knowledge_metric(
        "AD_CTR", marketplace="wb", source_field="spend"
    ) is None


def test_wb_orders_cabinet_binding_records_partial_not_equal_to_order_feed():
    orders = get_knowledge_metric(
        "ORDERS", marketplace="wb", source_field="finishedPrice"
    )

    assert orders is not None
    assert orders["cabinet_binding"]["surface_ru"] == "Аналитика продавца → Лента заказов"
    assert orders["cabinet_binding"]["label_ru"] == "Все заказы"
    assert orders["cabinet_binding"]["equivalence_status"] == "partial"
    assert "не эквивалентный" in orders["cabinet_binding"]["scope_ru"]
    assert "/api/v1/supplier/orders" in orders["cabinet_binding"]["scope_ru"]
    assert "Не приравнивать" in orders["guardrail"]


def test_partial_cabinet_binding_is_not_promoted_to_verified():
    catalog = load_marketplace_knowledge_catalog()
    orders = catalog["metrics"]["WB_ORDERS"]

    assert orders["semantic_status"] == "verified"
    assert orders["cabinet_binding"]["equivalence_status"] == "partial"
    assert orders["cabinet_binding"]["equivalence_status"] != "verified"


def test_wb_orders_binding_detects_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["ORDERS"]["provider_mappings"]["wb"]["fields"] = [
        "date",
        "isCancel",
    ]

    with pytest.raises(MarketplaceKnowledgeError, match="finishedPrice"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_wb_fbs_stock_binding_detects_amount_field_drift():
    catalog = load_marketplace_knowledge_catalog()
    registry = deepcopy(load_metric_registry())
    registry["metrics"]["CURRENT_FBS_STOCK"]["provider_mappings"]["wb"]["fields"] = [
        "chrtId",
    ]

    with pytest.raises(MarketplaceKnowledgeError, match="amount"):
        validate_knowledge_against_metric_registry(catalog, registry)


def test_weekly_report_field_coverage_is_complete_92_of_92():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    weekly = result["weekly_report_field_coverage"]

    assert weekly["dataset_id"] == "wb_weekly_finance_main"
    assert weekly["physical_field_count"] == 92
    assert weekly["catalogued_field_count"] == 92
    assert weekly["reviewed_field_count"] == 92
    assert weekly["physical_schema_matches_catalog"] is True
    assert weekly["field_coverage_complete"] is True
    assert weekly["missing_meaning_fields"] == []
    assert weekly["missing_safe_uses_fields"] == []
    assert weekly["unreviewed_fields"] == []
    assert weekly["capability_linked_field_count"] == 92
    assert weekly["capability_unlinked_field_count"] == 0
    assert weekly["capability_unlinked_fields"] == []
    assert "agencyVat" in weekly["blocked_unapproved_provider_fields"]


def test_report_field_knowledge_exposes_human_meaning_and_guardrails():
    payout = get_report_field_knowledge("paymentSchedule")
    assert payout["semantic_status"] == "REVIEWED"
    assert payout["role"] == "measure"
    assert "Вывести сейчас" in payout["meaning_ru"]
    assert any("график выплат" in item.lower() for item in payout["limitations"])

    order_date = get_report_field_knowledge("orderDt")
    assert "оформления заказа" in order_date["meaning_ru"]
    assert any("полного потока заказов" in item for item in order_date["limitations"])


def test_weekly_report_role_breakdown_covers_all_fields():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    weekly = result["weekly_report_field_coverage"]
    assert sum(weekly["role_counts"].values()) == 92
    assert weekly["semantic_status_counts"] == {"REVIEWED": 92}


def test_weekly_field_coverage_exposes_final_seven_field_routes():
    result = verify_marketplace_knowledge(today=date(2026, 9, 23), max_source_age_days=30)
    by_field = result["weekly_report_field_coverage"]["capabilities_by_field"]

    for field in (
        "salePercent",
        "productDiscountForReport",
        "sellerPromo",
        "supRatingUp",
        "isKgvpV2",
    ):
        assert "legacy_finance_diagnostics" in by_field[field]
    assert "payout_term_change_fee" in by_field["paymentSchedule"]
    assert "social_certificate_payment" in by_field["paidWithSocialCertificate"]
