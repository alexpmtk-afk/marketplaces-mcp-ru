from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from core.business_router import execute_business_query
from core.order_history import MemoryOrderHistoryStore, OrderHistoryCoverage
from core.registry import EndpointSpec


class _Store:
    def resolve_named(self, service, fields, env_map, name):
        return {"token": "fake"}, name


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(
            name="wb", fields=["token"], env_map={"token": "WB_API_TOKEN"}, store=_Store()
        )

    async def call_spec(self, spec, *, query=None, creds_override=None, **kwargs):
        self.calls.append((spec.operation_id, dict(query or {})))
        return self.responses.pop(0)


class _Catalog:
    def __init__(self):
        self.spec = EndpointSpec(
            operation_id="wb_stats_orders", method="GET",
            host="statistics-api.wildberries.ru", path="/api/v1/supplier/orders",
            section="statistics", scope="statistics", safety="read",
            pagination="lastchangedate", rate_limit="1 req/min", quota_proven=True,
        )

    def get(self, operation_id):
        return self.spec if operation_id == "wb_stats_orders" else None


def _wb(responses):
    async def exact_day(*args, **kwargs):
        raise AssertionError("exact-day route not expected")
    return SimpleNamespace(
        client=_Client(responses), catalog=_Catalog(), wb_get_orders_summary=exact_day,
    )


def _row(srid, day, price, *, cancelled=False, changed_hour=12):
    return {
        "srid": srid,
        "date": day.isoformat() + "T10:00:00",
        "lastChangeDate": day.isoformat() + f"T{changed_hour:02d}:00:00",
        "isCancel": cancelled,
        "finishedPrice": price,
    }


def _covered_store(start, end, rows):
    store = MemoryOrderHistoryStore()
    store.upsert_rows("wb_dmitrieva", rows)
    store.save_coverage(OrderHistoryCoverage(
        cabinet="wb_dmitrieva",
        target_from=start.isoformat(),
        covered_from=start.isoformat(),
        covered_to=end.isoformat(),
        watermark_last_change_date=end.isoformat() + "T23:00:00",
        last_successful_sync_at=datetime.now(timezone.utc).isoformat(),
        status="complete",
    ))
    return store


def test_old_period_is_answered_entirely_from_canonical_history_without_api_call():
    today = date.today()
    start = today - timedelta(days=150)
    end = today - timedelta(days=100)
    rows = [
        _row("h1", start + timedelta(days=2), 100),
        _row("h2", start + timedelta(days=3), 200, cancelled=True),
        _row("h3", end, 300),
    ]
    store = _covered_store(start, today - timedelta(days=1), rows)
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_order_history_store": store},
        marketplace="wb", metric="ORDERS", seller="Дмитриева",
        date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is True
    assert result["route"] == "historical_store"
    assert result["source"] == "wb_orders_history_ydb"
    assert result["orders_count"] == 2
    assert result["orders_amount"] == 400.0
    assert result["cancelled_orders_excluded"] == 1
    assert wb.client.calls == []


def test_long_period_is_split_between_history_and_current_statistics_automatically():
    today = date.today()
    oldest_live = today - timedelta(days=89)
    start = today - timedelta(days=120)
    end = today - timedelta(days=1)
    history_end = oldest_live - timedelta(days=1)
    history_rows = [
        _row("h1", start + timedelta(days=1), 100),
        _row("h2", history_end, 150),
    ]
    store = _covered_store(start, today - timedelta(days=1), history_rows)
    live_rows = [
        _row("l1", oldest_live, 200),
        _row("l2", end, 300),
        _row("l3", end, 999, cancelled=True),
    ]
    wb = _wb([{"ok": True, "data": live_rows}])

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_order_history_store": store},
        marketplace="wildberries", metric="orders", seller="wb_dmitrieva",
        date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is True
    assert result["route"] == "historical_plus_operational"
    assert result["source"] == "wb_orders_history_ydb+wb_stats_orders"
    assert result["orders_count"] == 4
    assert result["orders_amount"] == 750.0
    assert result["cancelled_orders_excluded"] == 1
    assert result["complete"] is True
    assert wb.client.calls == [
        ("wb_stats_orders", {"dateFrom": oldest_live.isoformat() + "T00:00:00", "flag": 0})
    ]


def test_missing_historical_coverage_fails_closed_and_does_not_query_live_segment():
    today = date.today()
    start = today - timedelta(days=150)
    end = today - timedelta(days=1)
    store = MemoryOrderHistoryStore()
    store.save_coverage(OrderHistoryCoverage(
        cabinet="wb_dmitrieva",
        target_from=(today - timedelta(days=120)).isoformat(),
        covered_from=(today - timedelta(days=120)).isoformat(),
        covered_to=(today - timedelta(days=1)).isoformat(),
        watermark_last_change_date=(today - timedelta(days=1)).isoformat() + "T23:00:00",
        last_successful_sync_at=datetime.now(timezone.utc).isoformat(),
        status="complete",
    ))
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_order_history_store": store},
        marketplace="wb", metric="ORDERS", seller="wb_dmitrieva",
        date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "coverage_gap"
    assert result["details"]["covered_from"] == (today - timedelta(days=120)).isoformat()
    assert wb.client.calls == []


def test_old_period_without_history_store_explains_required_server_capability():
    today = date.today()
    start = today - timedelta(days=120)
    end = today - timedelta(days=1)
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_order_history_store": None},
        marketplace="wb", metric="ORDERS", seller="wb_dmitrieva",
        date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert "MARKETPLACE_MCP_YDB_ENDPOINT" in result["details"]["requires"]
    assert wb.client.calls == []
