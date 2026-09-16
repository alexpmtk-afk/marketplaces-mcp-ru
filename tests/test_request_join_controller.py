from __future__ import annotations

from copy import deepcopy
from datetime import date

from core import request_source_system_map  # noqa: F401
from core.request_execution_controller import control_marketplace_execution
from core.request_join_controller import (
    CALCULATION_CONTRACT_VERSION,
    JOIN_BLOCKED,
    JOIN_CLARIFICATION_REQUIRED,
    JOIN_CONTRACT_VERSION,
    JOIN_CONTROLLER_VERSION,
    JOIN_READY,
    control_marketplace_join,
)
from core.system_map import SYSTEM_MAP

TODAY = date(2026, 9, 16)
QUESTION = "Сравни продажи и рекламные расходы за август"
BASE = {
    "marketplace": "wb",
    "seller": "wb_novokshenov",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
}


def _execution(question: str = QUESTION, **kwargs):
    args = dict(BASE)
    args.update(kwargs)
    return control_marketplace_execution(question, today=TODAY, **args)


def _valid_leg_results(question: str = QUESTION):
    execution = _execution(question)
    assert execution["can_dispatch"] is True
    results = []
    for contract in execution["dispatch_contracts"]:
        target = contract["semantic_target"]
        if target["id"] == "sale_and_return_operations":
            payload = {
                "ok": True,
                "marketplace": "wb",
                "seller": "wb_novokshenov",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "capability_id": "sale_and_return_operations",
                "coverage": {"status": "FULL_COVERAGE"},
                "calculation": {
                    "sales_and_returns_by_currency": [
                        {
                            "currency": "RUB",
                            "sale_amount": 1000.0,
                            "return_amount": 100.0,
                            "net_sales_amount": 900.0,
                            "sale_units": 10.0,
                            "return_units": 1.0,
                            "net_units": 9.0,
                        }
                    ],
                    "cross_currency_total": None,
                },
                "provenance": {
                    "sum_rule": "sale_minus_return_by_doc_type",
                    "currency_rule": "never_sum_across_currencies",
                },
            }
        elif target["id"] == "advertising_performance":
            payload = {
                "ok": True,
                "marketplace": "wb",
                "seller": "wb_novokshenov",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
                "metric": "ADVERTISING_PERFORMANCE",
                "data_class": "ADVERTISING_ATTRIBUTION_OPERATIONAL",
                "metric_contract_version": "wb_ads_m0.v1",
                "coverage": {"status": "FULL_COVERAGE"},
                "metrics": {
                    "spend": 150.0,
                    "attributed_order_amount": 600.0,
                    "drr_order_pct": 25.0,
                    "roas": 4.0,
                },
                "semantic_resolution": {
                    "resolution_type": "CAPABILITY",
                    "capability_id": "advertising_performance",
                },
            }
        else:  # pragma: no cover - protects the fixture from silent planner growth
            raise AssertionError(target)
        results.append({"contract_id": contract["contract_id"], "result": payload})
    return results


def _join(question: str = QUESTION, **kwargs):
    args = dict(BASE)
    args["leg_results"] = _valid_leg_results(QUESTION)
    args.update(kwargs)
    return control_marketplace_join(question, today=TODAY, **args)


def test_registered_sales_and_advertising_comparison_is_side_by_side_only():
    result = _join()
    assert result["ok"] is True
    assert result["controller"] == JOIN_CONTROLLER_VERSION
    assert result["state"] == JOIN_READY
    assert result["can_join"] is True
    assert result["arithmetic_allowed"] is False
    assert result["calculation_contract"] is None
    assert result["calculation_contract_version"] == CALCULATION_CONTRACT_VERSION
    assert result["registered_cross_source_calculations"] == []
    contract = result["join_contract"]
    assert contract["contract_version"] == JOIN_CONTRACT_VERSION
    assert contract["contract_id"] == "wb_sales_vs_advertising_side_by_side.v1"
    assert contract["comparison_mode"] == "SIDE_BY_SIDE_ONLY"
    assert contract["arithmetic_allowed"] is False
    assert contract["provenance_required"] is True
    assert "currency alignment is not inferred" in contract["currency_policy"]
    assert len(result["joined_legs"]) == 2


def test_missing_required_leg_result_fails_closed():
    leg_results = _valid_leg_results()
    result = control_marketplace_join(
        QUESTION,
        today=TODAY,
        leg_results=leg_results[:1],
        **BASE,
    )
    assert result["state"] == JOIN_BLOCKED
    assert result["can_join"] is False
    assert any(item["type"] == "REQUIRED_LEG_RESULT_MISSING" for item in result["blockers"])


def test_partial_coverage_fails_closed_before_join():
    leg_results = _valid_leg_results()
    broken = deepcopy(leg_results)
    broken[0]["result"]["coverage"]["status"] = "PARTIAL_COVERAGE"
    result = control_marketplace_join(QUESTION, today=TODAY, leg_results=broken, **BASE)
    assert result["state"] == JOIN_BLOCKED
    assert any(item["type"] == "COVERAGE_GATE_NOT_PROVEN" for item in result["blockers"])


def test_semantic_target_mismatch_fails_closed():
    leg_results = _valid_leg_results()
    broken = deepcopy(leg_results)
    sales = next(item for item in broken if item["result"].get("capability_id") == "sale_and_return_operations")
    sales["result"]["capability_id"] = "penalties"
    result = control_marketplace_join(QUESTION, today=TODAY, leg_results=broken, **BASE)
    assert result["state"] == JOIN_BLOCKED
    assert any(item["type"] == "SEMANTIC_TARGET_MISMATCH" for item in result["blockers"])


def test_wrong_seller_or_period_cannot_be_joined():
    leg_results = _valid_leg_results()
    wrong_seller = deepcopy(leg_results)
    wrong_seller[0]["result"]["seller"] = "wb_other"
    result = control_marketplace_join(QUESTION, today=TODAY, leg_results=wrong_seller, **BASE)
    assert any(item["type"] == "SELLER_SCOPE_MISMATCH" for item in result["blockers"])

    wrong_period = deepcopy(leg_results)
    wrong_period[0]["result"]["date_to"] = "2026-08-30"
    result = control_marketplace_join(QUESTION, today=TODAY, leg_results=wrong_period, **BASE)
    assert any(item["type"] == "PERIOD_SCOPE_MISMATCH" for item in result["blockers"])


def test_cross_source_drr_request_requires_registered_calculation_contract():
    question = "Рассчитай DRR по продажам и рекламным расходам за август"
    result = control_marketplace_join(question, today=TODAY, leg_results=[], **BASE)
    assert result["state"] == JOIN_BLOCKED
    assert result["arithmetic_allowed"] is False
    assert any(item["type"] == "CALCULATION_CONTRACT_REQUIRED" for item in result["blockers"])


def test_client_cannot_name_an_unregistered_formula():
    result = control_marketplace_join(
        QUESTION,
        today=TODAY,
        leg_results=_valid_leg_results(),
        calculation_id="overall_drr.v1",
        **BASE,
    )
    assert result["state"] == JOIN_BLOCKED
    assert any(item["type"] == "CALCULATION_CONTRACT_NOT_REGISTERED" for item in result["blockers"])


def test_unregistered_public_card_stock_join_remains_blocked():
    result = control_marketplace_join(
        "Сравни цену на сайте и текущие остатки Wildberries",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
        target_ids=["wb-card-1"],
        leg_results=[],
        today=TODAY,
    )
    assert result["state"] == JOIN_BLOCKED
    assert any(item["type"] == "JOIN_CONTRACT_NOT_REGISTERED" for item in result["blockers"])


def test_single_source_request_does_not_need_join_controller():
    result = control_marketplace_join(
        "Сколько остатков сейчас на Wildberries?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
        leg_results=[],
        today=TODAY,
    )
    assert result["state"] == JOIN_BLOCKED
    assert any(item["type"] == "JOIN_NOT_REQUIRED" for item in result["blockers"])


def test_clarification_state_propagates_without_join_attempt():
    result = control_marketplace_join(
        "Сколько заказов сегодня?",
        seller="wb_novokshenov",
        leg_results=[],
        today=TODAY,
    )
    assert result["state"] == JOIN_CLARIFICATION_REQUIRED
    assert result["can_join"] is False


def test_system_map_declares_join_controller_and_zero_cross_source_formulas():
    join = SYSTEM_MAP["request_source_router"]["join_controller"]
    assert join["status"] == "REGISTERED_JOIN_CONTRACTS_V1"
    assert join["runtime_entry"] == "marketplace_join_control"
    assert join["controller_version"] == JOIN_CONTROLLER_VERSION
    assert join["join_contract_version"] == JOIN_CONTRACT_VERSION
    assert join["calculation_contract_version"] == CALCULATION_CONTRACT_VERSION
    assert join["registered_cross_source_calculations"] == []
    assert "side-by-side" in join["approved_v1_join"]
