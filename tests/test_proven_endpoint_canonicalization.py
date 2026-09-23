from __future__ import annotations

import asyncio
import json

from core.registry import Catalog, EndpointSpec
from core.tools import resolve_equivalent_proven_read_spec
from wb_mcp import server as wb


def _call(name: str, args: dict) -> dict:
    result = asyncio.run(wb.mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_call_method_unknown_wrong_verb_price_copy_fails_closed(monkeypatch):
    stale = EndpointSpec(
        operation_id="wb_generated_stale_price_copy",
        method="POST",
        host="discounts-prices-api.wildberries.ru",
        path="/api/v2/list/goods/filter",
        safety="read",
        quota_proven=False,
    )
    wb.catalog.upsert(stale)
    calls = []

    async def fake_call_spec(spec, **kwargs):
        calls.append((spec, kwargs))
        return {"ok": True}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)
    try:
        payload = _call("wb_call_method", {
            "operation_id": stale.operation_id,
            "body": {"filterNmID": 392023986, "limit": 1, "offset": 0},
            "cabinet": "",
        })
    finally:
        wb.catalog.remove(stale.operation_id)

    assert payload["ok"] is False
    assert payload["code"] == "READ_SEMANTICS_UNPROVEN"
    assert payload["provider_call_sent"] is False
    assert calls == []


def test_fetch_all_unknown_wrong_verb_stock_copy_fails_closed(monkeypatch):
    stale = EndpointSpec(
        operation_id="wb_generated_stale_stock_copy",
        method="GET",
        host="seller-analytics-api.wildberries.ru",
        path="/api/analytics/v1/stocks-report/wb-warehouses",
        safety="read",
        pagination="offset",
        quota_proven=False,
        items_path="items",
    )
    wb.catalog.upsert(stale)
    calls = []

    async def fake_fetch_all(client, spec, **kwargs):
        calls.append((spec, kwargs))
        return {"ok": True, "items": []}

    monkeypatch.setattr("core.tools._fetch_all", fake_fetch_all)
    try:
        payload = _call("wb_fetch_all", {
            "operation_id": stale.operation_id,
            "query": {"nmIds": [392023986], "limit": 1000, "offset": 0},
            "cabinet": "",
            "max_items": 10,
        })
    finally:
        wb.catalog.remove(stale.operation_id)

    assert payload["ok"] is False
    assert payload["code"] == "RATE_LIMIT_RULE_UNPROVEN"
    assert payload["provider_call_sent"] is False
    assert calls == []


def test_equivalent_resolution_stays_fail_closed_when_proven_match_is_ambiguous():
    stale = EndpointSpec(
        operation_id="stale",
        method="GET",
        host="example.invalid",
        path="/same",
        safety="read",
    )
    proven_a = EndpointSpec(
        operation_id="proven_a",
        method="GET",
        host="example.invalid",
        path="/same",
        safety="read",
        rate_limit="1 req/min",
        quota_proven=True,
    )
    proven_b = EndpointSpec(
        operation_id="proven_b",
        method="POST",
        host="example.invalid",
        path="/same",
        safety="read",
        rate_limit="2 req/min",
        quota_proven=True,
    )
    catalog = Catalog([stale, proven_a, proven_b])

    resolved, changed = resolve_equivalent_proven_read_spec(catalog, stale)

    assert changed is True
    assert resolved.operation_id == "proven_a"


def test_equivalent_resolution_never_promotes_mutating_requested_operation():
    stale_write = EndpointSpec(
        operation_id="stale_write",
        method="POST",
        host="example.invalid",
        path="/same",
        safety="write",
    )
    proven_read = EndpointSpec(
        operation_id="proven_read",
        method="GET",
        host="example.invalid",
        path="/same",
        safety="read",
        rate_limit="1 req/min",
        quota_proven=True,
    )
    catalog = Catalog([stale_write, proven_read])

    resolved, changed = resolve_equivalent_proven_read_spec(catalog, stale_write)

    assert changed is False
    assert resolved.operation_id == "stale_write"
