from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from core.business_router import execute_business_query
from core.registry import EndpointSpec


class _Store:
    def resolve_named(self, service, fields, env_map, name):
        assert service == "wb"
        return {"token": "fake-token"}, name


class _Catalog:
    def __init__(self):
        self.spec = EndpointSpec(
            operation_id="wb_analytics_funnel",
            method="POST",
            host="seller-analytics-api.wildberries.ru",
            path="/api/analytics/v3/sales-funnel/products",
            section="analytics",
            scope="analytics",
            safety="read",
            pagination="offset",
            rate_limit="3 req/min",
            quota_proven=True,
            items_path="data.products",
        )

    def get(self, operation_id):
        return self.spec if operation_id == "wb_analytics_funnel" else None


class _Client:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []
        self.config = SimpleNamespace(
            name="wb",
            fields=["token"],
            env_map={"token": "WB_API_TOKEN"},
            store=_Store(),
        )

    async def call_spec(self, spec, *, json_body=None, creds_override=None, **kwargs):
        self.calls.append((spec.operation_id, dict(json_body or {}), dict(creds_override or {})))
        return self.pages.pop(0)


def _wb(pages):
    async def one_day(*args, **kwargs):
        raise AssertionError("one-day path was not expected")

    return SimpleNamespace(
        client=_Client(pages),
        catalog=_Catalog(),
        wb_get_orders_summary=one_day,
    )


def _product(order_count=10, order_sum=1000, cancel_count=2, cancel_sum=200):
    return {
        "product": {"nmId": 123},
        "statistic": {
            "selected": {
                "orderCount": order_count,
                "orderSum": order_sum,
                "cancelCount": cancel_count,
                "cancelSum": cancel_sum,
            }
        },
    }


def _ok(products):
    return {"ok": True, "data": {"data": {"products": products}}}


def test_multi_day_orders_choose_one_large_provider_period_and_aggregate():
    wb = _wb([_ok([_product()])])
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=179)

    result = asyncio.run(execute_business_query(
        {"wb": wb},
        marketplace="wb",
        metric="ORDERS",
        seller="wb_dmitrieva",
        date_from=start.isoformat(),
        date_to=end.isoformat(),
    ))

    assert result["ok"] is True
    assert result["route"] == "provider_aggregate"
    assert result["source"] == "wb_analytics_funnel"
    assert result["complete"] is True
    assert result["orders_count"] == 8
    assert result["orders_amount"] == 800.0
    assert result["gross_orders_count"] == 10
    assert result["cancelled_orders_count"] == 2
    assert len(wb.client.calls) == 1
    _, body, creds = wb.client.calls[0]
    assert body["selectedPeriod"] == {"start": start.isoformat(), "end": end.isoformat()}
    assert body["limit"] == 1000
    assert body["offset"] == 0
    assert creds == {"token": "fake-token"}


def test_orders_pagination_is_server_side_and_totals_cover_all_pages():
    first = [_product(order_count=1, order_sum=10, cancel_count=0, cancel_sum=0) for _ in range(1000)]
    second = [_product(order_count=2, order_sum=20, cancel_count=1, cancel_sum=5)]
    wb = _wb([_ok(first), _ok(second)])
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=30)

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wildberries", metric="orders",
        seller="Дмитриева", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is True
    assert result["pages_fetched"] == 2
    assert result["products_fetched"] == 1001
    assert result["orders_count"] == 1001
    assert result["orders_amount"] == 10015.0
    assert [call[1]["offset"] for call in wb.client.calls] == [0, 1000]


def test_partial_large_query_is_never_presented_as_complete():
    first = [_product(order_count=1, order_sum=10, cancel_count=0, cancel_sum=0) for _ in range(1000)]
    rate_limited = {
        "ok": False,
        "error": "rate_limit",
        "error_type": "rate_limit",
        "message": "busy",
        "retryable": True,
        "retry_after_seconds": 999,
    }
    wb = _wb([_ok(first), rate_limited])
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=30)

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["complete"] is False
    assert result["partial_result_discarded"] is True
    assert result["partial_products_fetched"] == 1000


def test_orders_older_than_provider_history_fail_closed_without_api_call():
    wb = _wb([])
    end = date.today() - timedelta(days=366)
    start = end - timedelta(days=10)

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert wb.client.calls == []


def test_long_sales_fail_closed_until_finance_semantics_are_approved():
    wb = _wb([])
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=179)

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="SALES",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert result["details"]["candidate_source"] == "wb_finance_sales_reports_detailed"
    assert wb.client.calls == []
