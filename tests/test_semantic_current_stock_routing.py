from __future__ import annotations

import asyncio
from datetime import date, timedelta

import core.semantic_business_router as router
import core.semantic_current_stock as stock
from core.semantic_current_stock import execute_current_stock_question
from core.semantic_resolver import resolve_semantic_question


def test_today_orders_use_operational_business_metric_not_generic_current_state():
    result = resolve_semantic_question("Сколько заказов сегодня?")
    assert result["resolution_type"] == "BUSINESS_METRIC"
    assert result["metric_id"] == "ORDERS"
    assert result["source_id"] == "wb_stats_orders"
    assert result["normalized_query"]["period_hint"] == "TODAY"
    assert result["execution_allowed"] is True


def test_today_commission_still_fails_closed_without_live_metric_contract():
    result = resolve_semantic_question("Какая комиссия Wildberries сегодня?")
    assert result["status"] == "REQUIRES_OTHER_SOURCE"
    assert result["route_id"] == "current_state_generic"
    assert result["execution_allowed"] is False


def test_current_stock_is_registered_as_live_business_metric():
    result = resolve_semantic_question("Сколько осталось товара на складах сейчас?")
    assert result["resolution_type"] == "BUSINESS_METRIC"
    assert result["metric_id"] == "CURRENT_STOCK"
    assert result["source_id"] == "wb_current_stocks"
    assert result["data_class"] == "CURRENT_OPERATIONAL_STOCK"
    assert result["normalized_query"]["measure"] == "UNITS"
    assert result["execution_allowed"] is True


def test_current_stock_product_grouping_is_approved():
    result = resolve_semantic_question("Покажи текущие остатки по товарам")
    assert result["resolution_type"] == "BUSINESS_METRIC"
    assert result["metric_id"] == "CURRENT_STOCK"
    assert result["normalized_query"]["grouping"] == "PRODUCT"
    assert result["execution_allowed"] is True


def test_business_router_dispatches_current_stock_executor(monkeypatch):
    captured = {}

    async def fake_current_stock(wb, **kwargs):
        captured["wb"] = wb
        captured.update(kwargs)
        return {
            "ok": True,
            "metric": "CURRENT_STOCK",
            "source": "wb_current_stocks",
            "stock_units": 42,
        }

    monkeypatch.setattr(router, "execute_current_stock_question", fake_current_stock)
    today = date.today().isoformat()
    wb = object()
    result = asyncio.run(router.execute_business_query(
        {"wb": wb},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from=today,
        date_to=today,
        question="Сколько текущих остатков?",
    ))

    assert result["ok"] is True
    assert captured["wb"] is wb
    assert captured["seller"] == "wb_novokshenov"
    assert captured["grouping"] == "TOTAL"
    assert result["semantic_resolution"]["metric_id"] == "CURRENT_STOCK"
    assert result["normalized_query"]["metric"] == "CURRENT_STOCK"


def test_historical_stock_request_fails_before_any_provider_access():
    yesterday = date.today() - timedelta(days=1)
    result = asyncio.run(execute_current_stock_question(
        object(),
        seller="wb_novokshenov",
        date_from=yesterday.isoformat(),
        date_to=yesterday.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert result["details"]["metric"] == "CURRENT_STOCK"
    assert result["details"]["required_source_id"] == "wb_historical_stock"
    assert result["details"]["rejected_substitute"] == "wb_current_stocks"


def test_current_wb_stock_exposes_cabinet_label_and_transit_separately(monkeypatch):
    async def fake_fetch_current_rows(wb, *, seller, nm_ids):
        assert seller == "wb_laser_master"
        assert nm_ids == [218395039]
        return seller, {
            "ok": True,
            "source": "wb_analytics_stocks_wb_warehouses",
            "items": [
                {
                    "nmId": 218395039,
                    "warehouseName": "Коледино",
                    "quantity": 3,
                    "inWayToClient": 8,
                    "inWayFromClient": 6,
                }
            ],
        }

    monkeypatch.setattr(stock, "_fetch_current_rows", fake_fetch_current_rows)

    result = asyncio.run(execute_current_stock_question(
        object(),
        seller="wb_laser_master",
        date_from="",
        date_to="",
        grouping="WAREHOUSE",
        nm_ids=[218395039],
    ))

    assert result["ok"] is True
    assert result["cabinet_label_ru"] == "Остатки «Склад WB»"
    assert result["stock_units"] == 3
    assert result["by_warehouse"] == [{"warehouse": "Коледино", "stock_units": 3}]
    assert result["in_transit_units"] == 14
    assert result["in_transit"] == {
        "label_ru": "Товары в пути",
        "units": 14,
        "to_client": {"label_ru": "Едут к покупателю", "units": 8},
        "from_client": {"label_ru": "Возвращаются на склад", "units": 6},
    }


def test_current_wb_stock_does_not_invent_transit_when_source_has_no_fields(monkeypatch):
    async def fake_fetch_current_rows(wb, *, seller, nm_ids):
        return seller, {
            "ok": True,
            "source": "wb_analytics_warehouse_remains_report",
            "items": [
                {"nmId": 218395039, "warehouseName": "Коледино", "quantity": 3}
            ],
        }

    monkeypatch.setattr(stock, "_fetch_current_rows", fake_fetch_current_rows)

    result = asyncio.run(execute_current_stock_question(
        object(),
        seller="wb_laser_master",
        date_from="",
        date_to="",
        nm_ids=[218395039],
    ))

    assert result["ok"] is True
    assert result["stock_units"] == 3
    assert result["transit_data_available"] is False
    assert "in_transit_units" not in result
