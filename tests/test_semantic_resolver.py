from core.semantic_resolver import load_semantic_intents, resolve_semantic_question


def test_fulfillment_question_uses_historical_delivery_method():
    result = resolve_semantic_question("По какой схеме отгружался товар 123?")
    assert result["status"] == "AVAILABLE_WITH_LIMITATION"
    assert result["capability_id"] == "observed_fulfillment_method"
    assert "deliveryMethod" in result["fields"]
    assert result["execution_allowed"] is False


def test_current_fulfillment_question_requires_other_source():
    result = resolve_semantic_question("Какая схема отгрузки сейчас у этого товара?")
    assert result["status"] == "REQUIRES_OTHER_SOURCE"
    assert result["concept_id"] == "current_fulfillment_configuration"
    assert result["next_action"] == "DO_NOT_QUERY_WEEKLY_ARCHIVE"


def test_all_orders_question_does_not_count_weekly_report_order_dates():
    result = resolve_semantic_question("Сколько заказов было за август?")
    assert result["status"] == "REQUIRES_OTHER_SOURCE"
    assert result["concept_id"] == "all_orders_placed"
    assert result["required_source_id"] == "wb_order_feed"


def test_order_date_question_is_allowed_as_reported_operation_attribute():
    result = resolve_semantic_question("Какая дата заказа у этой продажи?")
    assert result["status"] == "AVAILABLE_WITH_LIMITATION"
    assert result["capability_id"] == "order_context_attribute"
    assert "orderDt" in result["fields"]


def test_penalty_question_routes_to_penalty_fields():
    result = resolve_semantic_question("Сколько штрафов и за что их начислили?")
    assert result["status"] == "AVAILABLE"
    assert result["capability_id"] == "penalties"
    assert "penalty" in result["fields"]
    assert "bonusTypeName" in result["fields"]


def test_storage_charge_vs_detailed_storage_are_separated():
    charge = resolve_semantic_question("Сколько списали за хранение?")
    assert charge["capability_id"] == "storage_charge"
    assert charge["status"] == "AVAILABLE_WITH_LIMITATION"

    detailed = resolve_semantic_question("Как рассчитано хранение по дням?")
    assert detailed["status"] == "REQUIRES_OTHER_SOURCE"
    assert detailed["concept_id"] == "detailed_storage_calculation"
    assert detailed["required_source_id"] == "wb_paid_storage_report"


def test_ozon_question_is_reference_only_because_no_ozon_dataset_exists():
    result = resolve_semantic_question("Какие продажи были на Ozon за месяц?")
    assert result["status"] == "REQUIRES_OTHER_SOURCE"
    assert result["concept_id"] == "ozon_data"
    assert result["required_source_database_presence"] == "NOT_IN_DATABASE"


def test_exact_physical_field_question_returns_field_semantics():
    result = resolve_semantic_question("Что означает поле deliveryMethod?")
    assert result["resolution_type"] == "FIELD"
    assert result["field_name"] == "deliveryMethod"
    assert result["status"] == "AVAILABLE_WITH_LIMITATION"


def test_explicit_current_state_never_falls_back_to_historical_archive():
    result = resolve_semantic_question("Какая комиссия Wildberries сейчас?")
    assert result["status"] == "REQUIRES_OTHER_SOURCE"
    assert result["route_id"] == "current_state_generic"
    assert result["next_action"] == "DO_NOT_QUERY_WEEKLY_ARCHIVE"


def test_unknown_question_fails_closed():
    result = resolve_semantic_question("Какой цвет лучше выбрать для упаковки?")
    assert result["status"] == "UNKNOWN"
    assert result["execution_allowed"] is False
    assert result["next_action"] == "DO_NOT_GUESS_OR_QUERY_ARCHIVE"


def test_intent_catalog_validates_against_registry():
    intents = load_semantic_intents()
    assert intents["policy"]["fail_closed_on_unknown"] is True
    assert len(intents["routes"]) >= 20
