from __future__ import annotations

from datetime import date

from core.request_source_router import (
    EXECUTION_BLOCKED,
    EXECUTION_NEEDS_CONTEXT,
    EXECUTION_READY,
    EXECUTION_READY_WITH_GATES,
    SOURCE_CANONICAL_ARCHIVE,
    SOURCE_HYBRID,
    SOURCE_LIVE_CABINET_API,
    SOURCE_PUBLIC_MARKETPLACE,
    SOURCE_SYSTEM_INTERNAL,
    SOURCE_UNAVAILABLE,
    plan_marketplace_request,
)

TODAY = date(2026, 9, 16)


def _plan(question: str, **kwargs):
    return plan_marketplace_request(question, today=TODAY, **kwargs)


def test_current_stock_routes_to_named_cabinet_live_api():
    plan = _plan(
        "Сколько остатков сейчас на Wildberries?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["source_status"] == "AVAILABLE"
    assert plan["downstream_handler"] == "marketplace_business_query"


def test_historical_stock_is_unavailable_and_never_uses_today_snapshot():
    plan = _plan(
        "Сколько остатков было 1 сентября?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-01",
        date_to="2026-09-01",
    )
    assert plan["source_family"] == SOURCE_UNAVAILABLE
    assert plan["source_status"] == "HISTORICAL_SOURCE_ABSENT"
    assert plan["availability_facts"]["stock_historical_archive"] is False
    assert "today's stock snapshot" in plan["forbidden_substitutes"]


def test_recent_historical_orders_use_live_cabinet_not_archive():
    plan = _plan(
        "Сколько заказов было в августе?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["source_status"] == "AVAILABLE_WITH_LIMITATION"
    assert plan["availability_facts"]["orders_historical_archive"] is False
    assert "unverified historical store" in plan["forbidden_substitutes"]


def test_orders_older_than_live_window_are_unavailable_without_real_archive():
    plan = _plan(
        "Сколько заказов было в январе?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-01-01",
        date_to="2026-01-31",
    )
    assert plan["source_family"] == SOURCE_UNAVAILABLE
    assert plan["source_status"] == "NO_VERIFIED_ARCHIVE_HISTORY"
    assert "unverified YDB history scaffolding" in plan["forbidden_substitutes"]


def test_historical_advertising_routes_to_canonical_archive_with_coverage_gate():
    plan = _plan(
        "Какой ДРР и рекламные расходы были за период?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert plan["source_family"] == SOURCE_CANONICAL_ARCHIVE
    assert plan["source_status"] == "FULL_COVERAGE_REQUIRED"


def test_public_card_question_routes_to_public_observation_source():
    plan = _plan(
        "Какая сейчас цена этого товара на сайте Wildberries?",
        marketplace="wb",
    )
    assert plan["source_family"] == SOURCE_PUBLIC_MARKETPLACE
    assert plan["source_status"] == "AVAILABLE_IF_MONITORED"
    assert plan["downstream_handler"] == "card_monitor_get_latest"


def test_public_rules_need_public_source_not_private_cabinet_or_archive():
    plan = _plan("Какие сейчас официальные правила Wildberries для продавцов?")
    assert plan["source_family"] == SOURCE_PUBLIC_MARKETPLACE
    assert plan["source_status"] == "NOT_CONNECTED_ON_DEMAND"
    assert "seller cabinet API" in plan["forbidden_substitutes"]


def test_system_data_availability_question_stays_inside_mcp():
    plan = _plan("Какие данные есть в базе и какие источники сейчас доступны?")
    assert plan["source_family"] == SOURCE_SYSTEM_INTERNAL
    assert plan["downstream_handler"] == "marketplace_data_catalog"


def test_cross_domain_question_gets_multi_leg_plan_instead_of_one_fake_source():
    plan = _plan(
        "Сравни продажи и рекламные расходы за август",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    assert plan["source_family"] == SOURCE_HYBRID
    assert len(plan["legs"]) >= 2
    assert {leg["purpose"] for leg in plan["legs"]} >= {"advertising", "sales_or_finance"}


def test_current_ozon_private_question_knows_source_class_without_guessing_executor():
    plan = _plan(
        "Сколько заказов сегодня на Ozon?",
        marketplace="ozon",
        seller="ozon_shop",
        date_from="2026-09-16",
        date_to="2026-09-16",
    )
    assert plan["source_family"] == SOURCE_LIVE_CABINET_API
    assert plan["source_status"] == "SOURCE_CLASS_KNOWN_SEMANTIC_EXECUTOR_NOT_YET_WIRED"


def test_private_business_question_without_marketplace_requests_context():
    plan = _plan("Сколько заказов сегодня?", seller="shop")
    assert plan["source_family"] == SOURCE_UNAVAILABLE
    assert plan["source_status"] == "MARKETPLACE_REQUIRED"
    assert "marketplace" in plan["required_context"]


def test_v2_single_live_metric_is_directly_executable():
    plan = _plan(
        "Сколько остатков сейчас на Wildberries?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-16",
        date_to="2026-09-16",
    )
    execution = plan["execution_plan"]
    assert plan["planner"] == "marketplace_query_plan.v2"
    assert execution["version"] == "marketplace_execution_plan.v2"
    assert execution["mode"] == "SINGLE_SOURCE"
    assert execution["status"] == EXECUTION_READY
    assert execution["can_start_execution"] is True
    assert execution["legs"][0]["executor"] == "marketplace_business_query"
    assert execution["join"]["strategy"] == "NONE"


def test_v2_archive_metric_is_executable_only_after_coverage_gate():
    plan = _plan(
        "Сколько было продаж за август?",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    execution = plan["execution_plan"]
    assert execution["status"] == EXECUTION_READY_WITH_GATES
    assert execution["can_start_execution"] is True
    assert execution["can_answer_without_more_validation"] is False
    assert execution["legs"][0]["coverage_gate"] == "FULL_COVERAGE"


def test_v2_missing_marketplace_is_a_context_block_not_a_guess():
    plan = _plan("Сколько заказов сегодня?", seller="shop")
    execution = plan["execution_plan"]
    assert execution["status"] == EXECUTION_NEEDS_CONTEXT
    assert execution["can_start_execution"] is False
    assert execution["blockers"][0]["type"] == "MISSING_CONTEXT"
    assert "marketplace" in execution["blockers"][0]["details"]


def test_v2_known_source_without_executor_is_blocked():
    plan = _plan(
        "Сколько заказов сегодня на Ozon?",
        marketplace="ozon",
        seller="ozon_shop",
        date_from="2026-09-16",
        date_to="2026-09-16",
    )
    execution = plan["execution_plan"]
    assert execution["status"] == EXECUTION_BLOCKED
    assert execution["legs"][0]["source_family"] == SOURCE_LIVE_CABINET_API
    assert execution["legs"][0]["executor"] is None


def test_v2_hybrid_comparison_requires_both_legs_and_preserves_provenance():
    plan = _plan(
        "Сравни продажи и рекламные расходы за август",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
    )
    execution = plan["execution_plan"]
    assert execution["mode"] == "MULTI_SOURCE"
    assert execution["status"] == EXECUTION_READY_WITH_GATES
    assert execution["join"]["strategy"] == "SIDE_BY_SIDE_COMPARISON"
    assert execution["join"]["requires_all_required_legs"] is True
    assert execution["join"]["allow_partial_answer"] is False
    assert execution["join"]["arithmetic_allowed_without_explicit_semantic_contract"] is False
    assert {leg["purpose"] for leg in execution["legs"]} >= {"advertising", "sales_or_finance"}
    assert all(leg["executor"] == "marketplace_business_query" for leg in execution["legs"])


def test_v2_public_plus_historical_stock_blocks_whole_join_instead_of_substitution():
    plan = _plan(
        "Сравни цену на сайте и остатки товара на 1 сентября",
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-09-01",
        date_to="2026-09-01",
    )
    execution = plan["execution_plan"]
    assert execution["mode"] == "MULTI_SOURCE"
    assert execution["status"] == EXECUTION_BLOCKED
    stock_leg = next(leg for leg in execution["legs"] if leg["purpose"] == "current_stock")
    public_leg = next(leg for leg in execution["legs"] if leg["purpose"] == "public_card")
    assert stock_leg["source_family"] == SOURCE_UNAVAILABLE
    assert stock_leg["source_status"] == "HISTORICAL_SOURCE_ABSENT"
    assert public_leg["source_family"] == SOURCE_PUBLIC_MARKETPLACE
    assert execution["join"]["missing_required_leg_behavior"] == "FAIL_CLOSED"
