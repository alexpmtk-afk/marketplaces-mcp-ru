from __future__ import annotations

import asyncio

import core.semantic_business_router as router


def test_canonical_router_executes_ordinary_orders_as_approved_metric(monkeypatch):
    captured = {}

    async def fake_legacy(modules, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "metric": "ORDERS",
            "source": "wb_stats_orders",
            "orders_count": 7,
            "orders_amount": 1234.0,
            "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
        }

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Сколько заказов было за август?",
    ))

    assert result["ok"] is True
    assert captured["metric"] == "ORDERS"
    assert captured["question"] == ""
    assert result["semantic_resolution"]["resolution_type"] == "BUSINESS_METRIC"
    assert result["semantic_resolution"]["metric_id"] == "ORDERS"
    assert result["normalized_query"]["measure"] == "UNITS"
    assert result["normalized_query"]["period"] == {
        "date_from": "2026-08-01",
        "date_to": "2026-08-31",
    }


def test_complete_order_flow_is_not_silently_converted_to_operational_orders(monkeypatch):
    captured = {}

    async def fake_legacy(modules, **kwargs):
        captured.update(kwargs)
        return {"ok": False, "error_type": "source_not_suitable"}

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Покажи полный поток заказов за август",
    ))

    assert result["ok"] is False
    assert captured["question"] == "Покажи полный поток заказов за август"
    assert captured["metric"] == ""


def test_unapproved_order_grouping_fails_before_source_execution(monkeypatch):
    called = False

    async def fake_legacy(modules, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Сколько заказов было по товарам за август?",
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert called is False
    assert result["details"]["semantic_resolution"]["normalized_query"]["grouping"] == "PRODUCT"
