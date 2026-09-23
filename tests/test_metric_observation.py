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
