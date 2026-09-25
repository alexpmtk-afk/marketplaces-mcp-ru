from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta

import core.semantic_business_router as router
from core.user_facing import present_business_result


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


def test_canonical_router_materializes_today_for_orders_when_dates_are_omitted(monkeypatch):
    captured = {}

    async def fake_legacy(modules, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "metric": "ORDERS",
            "source": "wb_stats_orders",
            "orders_count": 1,
            "orders_amount": 100.0,
            "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
        }

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Сколько заказов сегодня?",
    ))

    today = date.today().isoformat()
    assert result["ok"] is True
    assert captured["date_from"] == today
    assert captured["date_to"] == today
    assert result["normalized_query"]["period"] == {
        "date_from": today,
        "date_to": today,
    }


def test_canonical_router_materializes_yesterday_for_orders_when_dates_are_omitted(monkeypatch):
    captured = {}

    async def fake_legacy(modules, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "metric": "ORDERS",
            "source": "wb_stats_orders",
            "orders_count": 1,
            "orders_amount": 100.0,
            "business_completeness": "PRELIMINARY_NOT_ALL_ORDERS",
        }

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Сколько заказов вчера?",
    ))

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    assert result["ok"] is True
    assert captured["date_from"] == yesterday
    assert captured["date_to"] == yesterday


def test_complete_order_flow_is_not_silently_converted_to_operational_orders(monkeypatch):
    called = False

    async def fake_legacy(modules, **kwargs):
        nonlocal called
        called = True
        return {"ok": True, "metric": "ORDERS"}

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
    assert result["error_type"] == "source_not_suitable"
    assert result["details"]["semantic_resolution"]["concept_id"] == "all_orders_placed"
    assert called is False


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


def test_canonical_router_executes_ozon_orders_before_generic_ozon_fallback(monkeypatch):
    captured = {}

    async def fake_ozon_orders(ozon, **kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "complete": True,
            "metric_id": "OZON_ORDERS",
            "value": 12,
            "unit": "UNITS",
        }

    monkeypatch.setattr(router, "execute_ozon_orders_question", fake_ozon_orders)

    result = asyncio.run(router.execute_business_query(
        {"ozon": object()},
        marketplace="ozon",
        seller="ozon_laser_master",
        date_from="2026-09-01",
        date_to="2026-09-25",
        question="Сколько заказов Ozon за сентябрь?",
    ))

    assert result["ok"] is True
    assert captured["question"] == "Сколько заказов Ozon за сентябрь?"
    assert captured["seller"] == "ozon_laser_master"
    assert captured["date_from"] == "2026-09-01"
    assert captured["date_to"] == "2026-09-25"
    assert result["semantic_resolution"]["metric_id"] == "OZON_ORDERS"
    assert result["semantic_resolution"]["route_id"] == "ozon_orders_postings"


def test_canonical_router_executes_ozon_postings_as_distinct_metric(monkeypatch):
    async def fake_ozon_orders(ozon, **kwargs):
        return {
            "ok": True,
            "complete": True,
            "metric_id": "OZON_POSTINGS",
            "value": 15,
            "unit": "UNITS",
        }

    monkeypatch.setattr(router, "execute_ozon_orders_question", fake_ozon_orders)

    result = asyncio.run(router.execute_business_query(
        {"ozon": object()},
        marketplace="ozon",
        seller="ozon_laser_master",
        question="Сколько отправлений Ozon за месяц?",
    ))

    assert result["ok"] is True
    assert result["semantic_resolution"]["metric_id"] == "OZON_POSTINGS"


class _SnapshotWb:
    async def wb_get_prices(self, **kwargs):
        assert kwargs["filter_nm_id"] == 218395039
        assert kwargs["cabinet"] == "wb_laser_master"
        return json.dumps({
            "ok": True,
            "source": "wb_prices_list",
            "products": [{
                "nm_id": 218395039,
                "vendor_code": "ТРН.01.045.9003.02.30",
                "currency": "RUB",
                "discount_percent": 20,
                "club_discount_percent": 5,
                "sizes": [{
                    "size_id": 1,
                    "tech_size": "0",
                    "price_amount": 4724,
                    "discounted_price_amount": 3779.2,
                    "club_discounted_price_amount": 3590.24,
                    "currency": "RUB",
                }],
            }],
            "price_scope": {
                "standard_current_price_fields_complete": True,
                "minimum_auto_promo_price": {
                    "status": "NOT_IN_APPROVED_CURRENT_PRICE_API",
                    "label_ru": "Минимальная цена для автоакций",
                },
                "promotion_entry_price": {
                    "status": "SEPARATE_PROMOTION_CONTEXT",
                    "label_ru": "Цена для участия в акции",
                },
            },
        }, ensure_ascii=False)

    async def _wb_fbs_stock_result(self, *, nm_id, cabinet):
        assert nm_id == 218395039
        assert cabinet == "wb_laser_master"
        return {
            "ok": True,
            "metric_id": "CURRENT_FBS_STOCK",
            "cabinet_label_ru": "Остатки «Свой склад»",
            "available_units": 4,
            "source": "wb_post_api_stocks_warehouseid",
            "warehouses": [{
                "warehouse_id": 10,
                "warehouse_name": "АНТ ГК",
                "available_units": 4,
            }],
        }


def test_generic_wb_product_prices_and_stocks_return_complete_cabinet_snapshot(monkeypatch):
    async def fake_current_stock(wb, **kwargs):
        assert kwargs["seller"] == "wb_laser_master"
        assert kwargs["nm_ids"] == [218395039]
        assert kwargs["grouping"] == "WAREHOUSE"
        return {
            "ok": True,
            "metric": "CURRENT_STOCK",
            "source": "wb_current_stocks",
            "as_of_date": "2026-09-24",
            "stock_units": 3,
            "by_warehouse": [{"warehouse": "Коледино", "stock_units": 3}],
            "in_transit": {
                "label_ru": "Товары в пути",
                "units": 14,
                "to_client": {"label_ru": "Едут к покупателю", "units": 8},
                "from_client": {"label_ru": "Возвращаются на склад", "units": 6},
            },
        }

    monkeypatch.setattr(router, "execute_current_stock_question", fake_current_stock)

    result = asyncio.run(router.execute_business_query(
        {"wb": _SnapshotWb()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Покажи цены и остатки по артикулу WB 218395039",
        nm_ids=[218395039],
    ))

    assert result["ok"] is True
    assert result["complete"] is True
    assert result["result_type"] == "WB_PRODUCT_CURRENT_SNAPSHOT"
    assert result["semantic_resolution"]["resolution_type"] == "BUSINESS_METRIC_SET"

    prices = result["prices"]
    assert prices["discount_percent"] == 20
    assert prices["club_discount_percent"] == 5
    assert [item["label_ru"] for item in prices["sizes"][0]["prices"]] == [
        "Цена продавца до скидки",
        "Цена со скидкой продавца",
        "Цена для WB Клуба",
    ]
    assert [item["amount"] for item in prices["sizes"][0]["prices"]] == [
        4724,
        3779.2,
        3590.24,
    ]
    assert prices["scope"]["minimum_auto_promo_price"]["status"] == (
        "NOT_IN_APPROVED_CURRENT_PRICE_API"
    )
    assert prices["scope"]["promotion_entry_price"]["status"] == "SEPARATE_PROMOTION_CONTEXT"

    stocks = result["stocks"]
    assert stocks["warehouse_wb"]["label_ru"] == "Остатки «Склад WB»"
    assert stocks["warehouse_wb"]["units"] == 3
    assert stocks["own_warehouse"]["label_ru"] == "Остатки «Свой склад»"
    assert stocks["own_warehouse"]["units"] == 4
    assert stocks["own_warehouse"]["warehouses"][0]["warehouse_name"] == "АНТ ГК"
    assert stocks["in_transit"]["units"] == 14
    assert stocks["in_transit"]["to_client"]["units"] == 8
    assert stocks["in_transit"]["from_client"]["units"] == 6

    shown = present_business_result(result)
    assert "Цена продавца до скидки: 4 724 ₽" in shown["user_message"]
    assert "Цена со скидкой продавца: 3779,2 ₽" in shown["user_message"]
    assert "Цена для WB Клуба: 3590,24 ₽" in shown["user_message"]
    assert "Остатки «Склад WB»: 3 шт." in shown["user_message"]
    assert "Остатки «Свой склад»: 4 шт. (АНТ ГК — 4 шт.)" in shown["user_message"]
    assert "Товары в пути: 14 шт. (Едут к покупателю — 8; Возвращаются на склад — 6)" in shown["user_message"]
    assert "Минимальная цена для автоакций" in shown["user_note"]
    assert "Цена для участия в акции" in shown["user_note"]


def test_generic_product_stock_without_price_returns_both_stock_buckets(monkeypatch):
    async def fake_current_stock(wb, **kwargs):
        return {
            "ok": True,
            "metric": "CURRENT_STOCK",
            "source": "wb_current_stocks",
            "stock_units": 3,
            "by_warehouse": [],
            "in_transit": {
                "label_ru": "Товары в пути",
                "units": 14,
                "to_client": {"label_ru": "Едут к покупателю", "units": 8},
                "from_client": {"label_ru": "Возвращаются на склад", "units": 6},
            },
        }

    monkeypatch.setattr(router, "execute_current_stock_question", fake_current_stock)

    result = asyncio.run(router.execute_business_query(
        {"wb": _SnapshotWb()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Какие остатки по артикулу 218395039?",
        nm_ids=[218395039],
    ))

    assert result["ok"] is True
    assert result["requested_parts"] == ["stocks"]
    assert "prices" not in result
    assert result["stocks"]["warehouse_wb"]["units"] == 3
    assert result["stocks"]["own_warehouse"]["units"] == 4


def test_explicit_wb_warehouse_stock_wording_keeps_single_bucket_route(monkeypatch):
    called = {}

    async def fake_current_stock(wb, **kwargs):
        called.update(kwargs)
        return {
            "ok": True,
            "metric": "CURRENT_STOCK",
            "source": "wb_current_stocks",
            "stock_units": 3,
        }

    monkeypatch.setattr(router, "execute_current_stock_question", fake_current_stock)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Сколько остатков на складе WB по артикулу 218395039?",
        nm_ids=[218395039],
    ))

    assert result["ok"] is True
    assert result["metric"] == "CURRENT_STOCK"
    assert result.get("result_type") != "WB_PRODUCT_CURRENT_SNAPSHOT"
    assert called["nm_ids"] == [218395039]


def test_complete_order_flow_never_falls_back_to_operational_orders(monkeypatch):
    called = {"legacy": False}

    async def fake_legacy(*args, **kwargs):
        called["legacy"] = True
        return {"ok": True, "metric": "ORDERS"}

    monkeypatch.setattr(router, "execute_legacy_business_query", fake_legacy)

    result = asyncio.run(router.execute_business_query(
        {"wb": object()},
        marketplace="wb",
        seller="wb_laser_master",
        question="Сколько всего оформленных заказов сегодня, включая неоплаченные?",
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    semantic = result["details"]["semantic_resolution"]
    assert semantic["resolution_type"] == "NOT_COVERED"
    assert semantic["concept_id"] == "all_orders_placed"
    assert semantic["required_source_id"] == "wb_order_feed"
    assert semantic["normalized_query"]["complete_order_flow"] is True
    assert called["legacy"] is False
