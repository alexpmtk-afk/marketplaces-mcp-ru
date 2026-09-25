from core.metric_observation import build_metric_observation


def test_metric_observation_uses_verified_human_label():
    observation = build_metric_observation(
        metric_id="WB_SELLER_PRICE_BEFORE_DISCOUNT",
        value=1500,
        unit="RUB",
        marketplace="wb",
        source_name="wb_prices_list",
        source_field="price",
        observed_at="2026-09-23T12:00:00Z",
    )

    assert observation.metric_id == "WB_SELLER_PRICE_BEFORE_DISCOUNT"
    assert observation.label == "Цена продавца до скидки"
    assert observation.value == 1500
    assert observation.unit == "RUB"
    assert observation.semantic_status == "verified"


def test_unknown_metric_is_visible_as_source_field_without_guessing():
    observation = build_metric_observation(
        metric_id="WB_UNKNOWN_NEW_PRICE_FIELD",
        value=777,
        unit="RUB",
        marketplace="wb",
        source_name="wb_prices_list",
        source_field="someNewPrice",
    )

    assert observation.label == "Поле источника: someNewPrice"
    assert observation.semantic_status == "source_field"
    assert "не подтверждено" in observation.definition


def test_current_stock_observation_uses_marketplace_specific_knowledge():
    wb = build_metric_observation(
        metric_id="CURRENT_STOCK",
        value=12,
        unit="UNITS",
        marketplace="wb",
        source_name="wb_current_stocks",
        source_field="quantity",
    )
    ozon = build_metric_observation(
        metric_id="CURRENT_STOCK",
        value=7,
        unit="UNITS",
        marketplace="ozon",
        source_name="ozon_current_stocks",
        source_field="stocks.present",
    )

    assert wb.label == "Текущие остатки на складах WB"
    assert wb.semantic_status == "verified"
    assert ozon.label == "Доступный остаток Ozon"
    assert ozon.semantic_status == "provisional"


def test_known_metric_unknown_provider_field_stays_unverified():
    observation = build_metric_observation(
        metric_id="CURRENT_STOCK",
        value=2,
        unit="UNITS",
        marketplace="ozon",
        source_name="ozon_current_stocks",
        source_field="stocks.reserved",
    )

    assert observation.label == "Поле источника: stocks.reserved"
    assert observation.semantic_status == "source_field"
