from __future__ import annotations

from datetime import date

from core.request_execution_controller import (
    CONTROL_BLOCKED,
    CONTROL_CLARIFICATION_REQUIRED,
    CONTROL_READY,
    CONTROL_READY_WITH_GATES,
    CONTROLLER_VERSION,
    LEG_CONTRACT_VERSION,
    control_marketplace_execution,
)

TODAY = date(2026, 9, 16)


def _control(question: str, **kwargs):
    return control_marketplace_execution(question, today=TODAY, **kwargs)


def test_single_current_stock_gets_one_exact_business_executor_contract():
    result = _control(
        "Сколько остатков сейчас на Wildberries?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
        nm_ids=[12345],
    )
    assert result["ok"] is True
    assert result["controller"] == CONTROLLER_VERSION
    assert result["state"] == CONTROL_READY
    assert result["can_dispatch"] is True
    assert len(result["dispatch_contracts"]) == 1
    contract = result["dispatch_contracts"][0]
    assert contract["contract_version"] == LEG_CONTRACT_VERSION
    assert contract["executor"] == "marketplace_business_query"
    assert contract["executor_arguments"] == {
        "marketplace": "wb",
        "seller": "wb_novokshenov",
        "date_from": "2026-09-16",
        "date_to": "2026-09-16",
        "question": "текущие остатки",
        "nm_ids": [12345],
    }
    assert contract["semantic_target"] == {"type": "BUSINESS_METRIC", "id": "CURRENT_STOCK"}
    assert contract["provenance_required"]["leg_id"] == contract["leg_id"]


def test_missing_marketplace_stops_before_any_dispatch_and_requests_clarification():
    result = _control("Сколько заказов сегодня?", seller="shop")
    assert result["ok"] is False
    assert result["state"] == CONTROL_CLARIFICATION_REQUIRED
    assert result["can_dispatch"] is False
    assert result["dispatch_contracts"] == []
    assert "marketplace" in result["required_context"]
    assert result["replan_required_after_clarification"] is True


def test_known_historical_stock_gap_is_blocked_not_disguised_as_clarification():
    result = _control(
        "Сколько остатков было 1 сентября?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-01",
        date_to="2026-09-01",
    )
    assert result["ok"] is False
    assert result["state"] == CONTROL_BLOCKED
    assert result["dispatch_contracts"] == []
    assert result["source_gap"]["source_status"] == "HISTORICAL_SOURCE_ABSENT"


def test_multisource_comparison_gets_distinct_narrow_questions_not_compound_copy():
    original = "Сравни продажи и рекламные расходы за август"
    result = _control(
        original,
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert result["ok"] is True
    assert result["state"] == CONTROL_READY_WITH_GATES
    assert result["can_dispatch"] is True
    contracts = result["dispatch_contracts"]
    assert len(contracts) == 2
    questions = {item["executor_arguments"]["question"] for item in contracts}
    assert questions == {"продажи", "рекламные расходы"}
    assert original not in questions
    assert all(item["original_compound_question_allowed"] is False for item in contracts)
    targets = {(item["semantic_target"]["type"], item["semantic_target"]["id"]) for item in contracts}
    assert targets == {
        ("CAPABILITY", "sale_and_return_operations"),
        ("CAPABILITY", "advertising_performance"),
    }


def test_multisource_join_is_fail_closed_and_never_authorizes_arithmetic_by_default():
    result = _control(
        "Сравни продажи и рекламные расходы за август",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    join = result["join_control"]
    assert join["requires_all_required_legs"] is True
    assert join["allow_partial_answer"] is False
    assert join["missing_required_leg_behavior"] == "FAIL_CLOSED"
    assert join["arithmetic_allowed"] is False
    assert join["explicit_calculation_contract_required_for_arithmetic"] is True
    assert join["provenance_required"] is True
    assert set(join["required_contract_ids"]) == {
        item["contract_id"] for item in result["dispatch_contracts"]
    }


def test_dispatch_policy_forbids_client_side_argument_rewrites():
    result = _control(
        "Сколько заказов было в августе?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert result["state"] == CONTROL_READY_WITH_GATES
    policy = result["dispatch_policy"]
    assert policy["use_only_returned_executor_and_arguments"] is True
    assert policy["do_not_rewrite_executor_arguments"] is True
    assert policy["do_not_pass_original_compound_question_to_each_leg"] is True
    contract = result["dispatch_contracts"][0]
    assert contract["executor_arguments"]["question"] == "заказы"
    assert contract["coverage_gate"] == "LIVE_RETENTION_WINDOW"


def test_public_card_without_product_target_blocks_all_dispatch_instead_of_returning_everything():
    result = _control(
        "Сравни цену на сайте и текущие остатки Wildberries",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
    )
    assert result["ok"] is False
    assert result["state"] == CONTROL_BLOCKED
    assert result["dispatch_contracts"] == []
    assert any(item["type"] == "PRODUCT_TARGET_REQUIRED" for item in result["blockers"])
    assert result["prepared_non_dispatchable_contracts"]


def test_public_card_with_target_can_dispatch_separately_from_current_stock():
    result = _control(
        "Сравни цену на сайте и текущие остатки Wildberries",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
        target_ids=["wb-card-1"],
    )
    assert result["ok"] is True
    assert result["state"] == CONTROL_READY_WITH_GATES
    executors = {item["executor"] for item in result["dispatch_contracts"]}
    assert executors == {"card_monitor_get_latest", "marketplace_business_query"}
    public = next(item for item in result["dispatch_contracts"] if item["executor"] == "card_monitor_get_latest")
    assert public["executor_arguments"]["target_ids"] == ["wb-card-1"]
    assert public["executor_arguments"]["marketplace"] == "wildberries"


def test_unrecognized_business_meaning_never_reaches_dispatch():
    result = _control(
        "Покажи мне коэффициент счастья товара за август",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert result["state"] == CONTROL_CLARIFICATION_REQUIRED
    assert result["dispatch_contracts"] == []
    assert (result["semantic_resolution"] or {}).get("resolution_type") == "UNKNOWN"
