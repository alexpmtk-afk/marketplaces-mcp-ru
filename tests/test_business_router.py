from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from types import SimpleNamespace

import core.business_router as router
from core.business_router import execute_business_query
from core.registry import EndpointSpec


class _Store:
    def resolve_named(self, service, fields, env_map, name):
        assert service == "wb"
        return {"token": "fake-token"}, name


class _Catalog:
    def __init__(self):
        self.spec = EndpointSpec(
            operation_id="wb_stats_orders",
            method="GET",
            host="statistics-api.wildberries.ru",
            path="/api/v1/supplier/orders",
            section="statistics",
            scope="statistics",
            safety="read",
            pagination="lastchangedate",
            rate_limit="1 req/min",
            quota_proven=True,
        )

    def get(self, operation_id):
        return self.spec if operation_id == "wb_stats_orders" else None


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(
            name="wb",
            fields=["token"],
            env_map={"token": "WB_API_TOKEN"},
            store=_Store(),
        )

    async def call_spec(self, spec, *, query=None, creds_override=None, **kwargs):
        self.calls.append((spec.operation_id, dict(query or {}), dict(creds_override or {}), kwargs))
        return self.responses.pop(0)


def _wb(responses, one_day_result=None):
    async def one_day(*args, **kwargs):
        if one_day_result is None:
            raise AssertionError("one-day path was not expected")
        return json.dumps(one_day_result)

    return SimpleNamespace(
        client=_Client(responses),
        catalog=_Catalog(),
        wb_get_orders_summary=one_day,
    )


def _row(day, *, price=100, cancelled=False, last_change=None):
    return {
        "srid": f"srid-{day.isoformat()}-{price}-{int(cancelled)}",
        "date": day.isoformat() + "T12:00:00",
        "lastChangeDate": (last_change or day).isoformat() + "T13:00:00",
        "isCancel": cancelled,
        "finishedPrice": price,
    }


def test_multi_day_orders_use_one_operational_statistics_range_and_aggregate():
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    rows = [
        _row(start, price=100),
        _row(start + timedelta(days=1), price=200, cancelled=True),
        _row(end, price=150),
        _row(start - timedelta(days=1), price=999),
        _row(end + timedelta(days=1), price=999),
    ]
    wb = _wb([{"ok": True, "data": rows}])

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="Дмитриева", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is True
    assert result["route"] == "operational_range"
    assert result["source"] == "wb_stats_orders"
    assert result["source_validation"] == "official_operational_preliminary"
    assert result["business_completeness"] == "PRELIMINARY_NOT_ALL_ORDERS"
    assert result["complete"] is True
    assert result["orders_count"] == 2
    assert result["orders_amount"] == 250.0
    assert result["cancelled_orders_excluded"] == 1
    assert result["provider_rows_in_period"] == 3
    assert len(wb.client.calls) == 1
    operation, query, creds, kwargs = wb.client.calls[0]
    assert operation == "wb_stats_orders"
    assert query == {"dateFrom": start.isoformat() + "T00:00:00", "flag": 0}
    assert creds == {"token": "fake-token"}
    assert kwargs["retry_on_429"] is False


def test_orders_over_operational_history_fail_closed_without_substitution():
    wb = _wb([])
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=100)

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    assert result["details"]["canonical_source"] == "wb_stats_orders"
    assert "operational/preliminary" in result["details"]["source_limitations"]
    assert result["details"]["rejected_substitute"] == "wb_analytics_funnel"
    assert "parity mismatch" in result["details"]["rejected_reason"]
    assert wb.client.calls == []


def test_provider_page_ceiling_never_returns_partial_total(monkeypatch):
    monkeypatch.setattr(router, "WB_STATS_MAX_ROWS", 2)
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=2)
    wb = _wb([{"ok": True, "data": [_row(start), _row(end)]}])

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
    ))

    assert result["ok"] is False
    assert result["error_type"] == "execution_pending"
    assert result["details"]["complete"] is False
    assert result["details"]["rows_received"] == 2


def test_one_day_orders_keep_existing_exact_day_path_but_mark_preliminary():
    day = date.today() - timedelta(days=1)
    wb = _wb([], one_day_result={
        "ok": True,
        "orders_count": 4,
        "orders_amount": 1234.0,
        "source": "wb_stats_orders",
    })

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wildberries", metric="orders",
        seller="wb_dmitrieva", date_from=day.isoformat(), date_to=day.isoformat(),
    ))

    assert result["ok"] is True
    assert result["route"] == "operational_exact_day"
    assert result["source_validation"] == "official_operational_preliminary"
    assert result["business_completeness"] == "PRELIMINARY_NOT_ALL_ORDERS"
    assert result["complete"] is True
    assert wb.client.calls == []


def test_product_filtered_orders_fail_closed_until_contract_is_approved():
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=6)
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb}, marketplace="wb", metric="ORDERS",
        seller="wb_dmitrieva", date_from=start.isoformat(), date_to=end.isoformat(),
        nm_ids=[123],
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


def test_natural_storage_question_routes_to_gated_semantic_archive(monkeypatch):
    wb = _wb([])
    archive_store = object()
    captured = {}

    async def fake_execute(store, **kwargs):
        captured["store"] = store
        captured.update(kwargs)
        return {
            "ok": True,
            "capability_id": "storage_charge",
            "calculation": {"totals_by_currency": [{"currency": "RUB", "amount": 42.0}]},
        }

    monkeypatch.setattr(router, "execute_semantic_archive_question", fake_execute)

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_archive_store": archive_store},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Сколько списали за хранение за август?",
    ))

    assert result["ok"] is True
    assert result["route"] == "semantic_archive"
    assert result["capability_id"] == "storage_charge"
    assert result["semantic_question"] == "Сколько списали за хранение за август?"
    assert captured["store"] is archive_store
    assert captured["seller"] == "wb_novokshenov"
    assert wb.client.calls == []


def test_natural_all_orders_question_never_uses_preliminary_statistics_feed():
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb},
        marketplace="wb",
        seller="wb_dmitrieva",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Сколько всего заказов было за август?",
    ))

    assert result["ok"] is False
    assert result["error_type"] == "source_not_suitable"
    resolution = result["details"]["semantic_resolution"]
    assert resolution["status"] == "REQUIRES_OTHER_SOURCE"
    assert resolution["concept_id"] == "all_orders_placed"
    assert resolution["required_source_id"] == "wb_order_feed"
    assert wb.client.calls == []


def test_natural_sales_question_routes_to_approved_semantic_archive(monkeypatch):
    wb = _wb([])
    archive_store = object()
    captured = {}

    async def fake_execute(store, **kwargs):
        captured["store"] = store
        captured.update(kwargs)
        return {
            "ok": True,
            "capability_id": "sale_and_return_operations",
            "calculation": {
                "sales_and_returns_by_currency": [
                    {"currency": "RUB", "net_sales_amount": 12345.0}
                ]
            },
        }

    monkeypatch.setattr(router, "execute_semantic_archive_question", fake_execute)

    result = asyncio.run(execute_business_query(
        {"wb": wb, "_archive_store": archive_store},
        marketplace="wb",
        seller="wb_novokshenov",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Какая была сумма продаж за август?",
    ))

    assert result["ok"] is True
    assert result["route"] == "semantic_archive"
    assert result["capability_id"] == "sale_and_return_operations"
    assert result["semantic_question"] == "Какая была сумма продаж за август?"
    assert captured["store"] is archive_store
    assert captured["seller"] == "wb_novokshenov"
    assert captured["date_from"].isoformat() == "2026-08-01"
    assert captured["date_to"].isoformat() == "2026-08-31"
    assert wb.client.calls == []


def test_question_takes_precedence_over_conflicting_legacy_metric():
    wb = _wb([])

    result = asyncio.run(execute_business_query(
        {"wb": wb},
        marketplace="wb",
        seller="wb_dmitrieva",
        date_from="2026-08-01",
        date_to="2026-08-31",
        question="Сколько всего заказов было за август?",
        metric="SALES",
    ))

    assert result["ok"] is False
    resolution = result["details"]["semantic_resolution"]
    assert resolution["concept_id"] == "all_orders_placed"
    assert wb.client.calls == []
