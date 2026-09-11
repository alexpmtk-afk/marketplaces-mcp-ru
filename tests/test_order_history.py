from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import core.order_history as history
from core.order_history import (
    MemoryOrderHistoryStore,
    OrderHistoryCoverage,
    canonical_order_totals,
    normalize_wb_order_rows,
    sync_wb_orders_history,
)
from core.registry import EndpointSpec


class _CredStore:
    def resolve_named(self, service, fields, env_map, name):
        assert service == "wb"
        return {"token": "fake"}, name


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(
            name="wb",
            fields=["token"],
            env_map={"token": "WB_API_TOKEN"},
            store=_CredStore(),
        )

    async def call_spec(self, spec, *, query=None, creds_override=None, **kwargs):
        self.calls.append((spec.operation_id, dict(query or {}), dict(creds_override or {}), kwargs))
        return self.responses.pop(0)


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


def _wb(responses):
    return SimpleNamespace(client=_Client(responses), catalog=_Catalog())


def _row(srid, day, *, last_change=None, cancelled=False, price=100.0):
    changed = last_change or (day.isoformat() + "T12:00:00")
    return {
        "srid": srid,
        "date": day.isoformat() + "T10:00:00",
        "lastChangeDate": changed,
        "isCancel": cancelled,
        "finishedPrice": price,
        "nmId": 123,
        "supplierArticle": "A-1",
    }


def test_normalize_keeps_freshest_state_for_same_srid():
    day = date.today() - timedelta(days=5)
    rows = normalize_wb_order_rows([
        _row("x", day, last_change=day.isoformat() + "T10:00:00", cancelled=False),
        _row("x", day, last_change=day.isoformat() + "T11:00:00", cancelled=True),
    ])
    assert len(rows) == 1
    assert rows[0]["isCancel"] is True
    assert rows[0]["lastChangeDate"].endswith("11:00:00")


def test_memory_store_newer_cancellation_replaces_old_state():
    store = MemoryOrderHistoryStore()
    day = date.today() - timedelta(days=5)
    store.upsert_rows("wb_dmitrieva", [
        _row("x", day, last_change=day.isoformat() + "T10:00:00", cancelled=False, price=500),
    ])
    store.upsert_rows("wb_dmitrieva", [
        _row("x", day, last_change=day.isoformat() + "T11:00:00", cancelled=True, price=500),
    ])
    rows = store.read_rows("wb_dmitrieva", day, day)
    totals = canonical_order_totals(rows, day, day)
    assert len(rows) == 1
    assert totals["orders_count"] == 0
    assert totals["orders_amount"] == 0.0
    assert totals["cancelled_orders_excluded"] == 1


def test_initial_sync_bootstraps_verified_coverage_and_watermark():
    store = MemoryOrderHistoryStore()
    day = date.today() - timedelta(days=3)
    wb = _wb([{"ok": True, "data": [
        _row("a", day, last_change=day.isoformat() + "T12:00:00", price=100),
        _row("b", day, last_change=day.isoformat() + "T13:00:00", price=200),
    ]}])

    result = asyncio.run(sync_wb_orders_history(
        wb, store, seller="wb_dmitrieva", bootstrap_days=30,
    ))

    assert result["ok"] is True
    assert result["stored_rows"] == 2
    coverage = store.coverage("wb_dmitrieva")
    assert coverage.status == "complete"
    assert coverage.covered_from == (date.today() - timedelta(days=29)).isoformat()
    assert coverage.covered_to == (date.today() - timedelta(days=1)).isoformat()
    assert coverage.watermark_last_change_date.endswith("13:00:00")
    assert wb.client.calls[0][1]["flag"] == 0
    assert wb.client.calls[0][1]["dateFrom"].endswith("T00:00:00")


def test_incremental_sync_continues_from_stored_watermark():
    store = MemoryOrderHistoryStore()
    day = date.today() - timedelta(days=5)
    first = _row("a", day, last_change=day.isoformat() + "T12:00:00", cancelled=False)
    wb1 = _wb([{"ok": True, "data": [first]}])
    asyncio.run(sync_wb_orders_history(wb1, store, seller="wb_dmitrieva", bootstrap_days=10))
    watermark = store.coverage("wb_dmitrieva").watermark_last_change_date

    changed = _row("a", day, last_change=day.isoformat() + "T14:00:00", cancelled=True)
    wb2 = _wb([{"ok": True, "data": [changed]}])
    result = asyncio.run(sync_wb_orders_history(wb2, store, seller="wb_dmitrieva"))

    assert result["ok"] is True
    assert wb2.client.calls[0][1]["dateFrom"] == watermark
    rows = store.read_rows("wb_dmitrieva", day, day)
    assert len(rows) == 1
    assert rows[0]["isCancel"] is True


def test_page_ceiling_persists_rows_but_does_not_claim_complete(monkeypatch):
    monkeypatch.setattr(history, "WB_STATS_MAX_ROWS", 2)
    store = MemoryOrderHistoryStore()
    day = date.today() - timedelta(days=2)
    wb = _wb([{"ok": True, "data": [
        _row("a", day, last_change=day.isoformat() + "T12:00:00"),
        _row("b", day, last_change=day.isoformat() + "T13:00:00"),
    ]}])

    result = asyncio.run(sync_wb_orders_history(wb, store, seller="wb_dmitrieva"))

    assert result["ok"] is False
    assert result["error_type"] == "execution_pending"
    assert result["details"]["complete"] is False
    assert store.count("wb_dmitrieva") == 2
    coverage = store.coverage("wb_dmitrieva")
    assert coverage.status == "syncing"
    assert coverage.covered_from is None
    assert coverage.watermark_last_change_date.endswith("13:00:00")


def test_stale_sync_marks_gap_before_sending_provider_request():
    store = MemoryOrderHistoryStore()
    old = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
    store.save_coverage(OrderHistoryCoverage(
        cabinet="wb_dmitrieva",
        target_from="2026-01-01",
        covered_from="2026-01-01",
        covered_to="2026-05-01",
        watermark_last_change_date="2026-05-01T12:00:00",
        last_successful_sync_at=old,
        status="complete",
    ))
    wb = _wb([])

    result = asyncio.run(sync_wb_orders_history(wb, store, seller="wb_dmitrieva"))

    assert result["ok"] is False
    assert result["error_type"] == "coverage_gap"
    assert store.coverage("wb_dmitrieva").status == "gap"
    assert wb.client.calls == []
