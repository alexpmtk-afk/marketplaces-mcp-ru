from __future__ import annotations

from datetime import date

from core.request_execution_controller import CONTROL_BLOCKED, CONTROL_READY, control_marketplace_execution
from core.request_source_router import (
    EXECUTION_BLOCKED,
    EXECUTION_READY,
    SOURCE_LIVE_CABINET_API,
    SOURCE_UNAVAILABLE,
    plan_marketplace_request,
)

TODAY = date(2026, 9, 17)


def test_current_ozon_price_and_stock_is_one_ready_server_owned_leg():
    question = "Какая сейчас цена и остаток Ozon LaserMaster по артикулу 3276433388?"
    plan = plan_marketplace_request(question, marketplace="ozon", today=TODAY)

    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["source_status"] == "AVAILABLE"
    assert plan["downstream_handler"] == "marketplace_business_query"
    assert plan["semantic_resolution"]["resolution_type"] == "BUSINESS_METRIC_SET"
    assert set(plan["semantic_resolution"]["metric_ids"]) == {"CURRENT_SELLING_PRICE", "CURRENT_STOCK"}

    execution = plan["execution_plan"]
    assert execution["mode"] == "SINGLE_SOURCE"
    assert execution["status"] == EXECUTION_READY
    assert len(execution["legs"]) == 1
    leg = execution["legs"][0]
    assert leg["purpose"] == "ozon_current_snapshot"
    assert leg["executor"] == "marketplace_business_query"
    assert leg["semantic_target"] == {"type": "BUSINESS_METRIC_SET", "id": "ozon_current_snapshot"}


def test_current_ozon_price_only_uses_same_snapshot_executor():
    question = "Какая сейчас цена Ozon LaserMaster по артикулу 3276433388?"
    plan = plan_marketplace_request(question, marketplace="ozon", today=TODAY)
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["semantic_resolution"]["metric_id"] == "CURRENT_SELLING_PRICE"
    assert plan["execution_plan"]["legs"][0]["purpose"] == "ozon_current_snapshot"


def test_historical_ozon_snapshot_fails_closed_before_dispatch():
    question = "Какая была цена и остаток Ozon LaserMaster в августе?"
    plan = plan_marketplace_request(question, marketplace="ozon", today=TODAY)
    assert plan["source_family"] == SOURCE_UNAVAILABLE
    assert plan["source_status"] == "HISTORICAL_SOURCE_ABSENT"
    assert plan["execution_plan"]["status"] == EXECUTION_BLOCKED
    assert plan["execution_plan"]["legs"][0]["executor"] is None
    assert "current Ozon stock snapshot" in plan["forbidden_substitutes"]


def test_unsupported_current_ozon_orders_remain_blocked():
    plan = plan_marketplace_request(
        "Сколько заказов сегодня на Ozon?",
        marketplace="ozon",
        seller="ozon_shop",
        today=TODAY,
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["source_status"] == "SOURCE_CLASS_KNOWN_SEMANTIC_EXECUTOR_NOT_YET_WIRED"
    assert plan["execution_plan"]["status"] == EXECUTION_BLOCKED


def test_execution_controller_preserves_original_ozon_snapshot_question():
    question = "Какая сейчас цена и остаток Ozon LaserMaster по артикулу 3276433388?"
    result = control_marketplace_execution(question, marketplace="ozon", today=TODAY)

    assert result["ok"] is True
    assert result["state"] == CONTROL_READY
    assert result["can_dispatch"] is True
    assert len(result["dispatch_contracts"]) == 1
    contract = result["dispatch_contracts"][0]
    assert contract["executor"] == "marketplace_business_query"
    assert contract["executor_arguments"]["question"] == question
    assert contract["executor_arguments"]["marketplace"] == "ozon"
    assert contract["semantic_target"] == {"type": "BUSINESS_METRIC_SET", "id": "ozon_current_snapshot"}
    assert contract["original_compound_question_allowed"] is True


def test_execution_controller_blocks_historical_ozon_snapshot():
    result = control_marketplace_execution(
        "Какая была цена и остаток Ozon LaserMaster в августе?",
        marketplace="ozon",
        today=TODAY,
    )
    assert result["ok"] is False
    assert result["state"] == CONTROL_BLOCKED
    assert result["dispatch_contracts"] == []
    assert result["source_gap"]["source_status"] == "HISTORICAL_SOURCE_ABSENT"
