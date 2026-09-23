from __future__ import annotations

import asyncio
import json

from core.registry import Catalog, EndpointSpec
from core.tools import resolve_equivalent_proven_read_spec
from wb_mcp import server as wb


def _call(name: str, args: dict) -> dict:
    result = asyncio.run(wb.mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_call_method_unknown_stale_price_id_reuses_unique_proven_contract(monkeypatch):
    stale = EndpointSpec(
        operation_id="wb_generated_stale_price_copy",
        method="POST",
        host="discounts-prices-api.wildberries.ru",
        path="/api/v2/list/goods/filter",
        safety="read",
        quota_proven=False,
    )
    wb.catalog.upsert(stale)
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen["method"] = spec.method
        seen["query"] = kwargs.get("query")
        seen["json_body"] = kwargs.get("json_body")
        return {"ok": True, "status": 200, "data": {"listGoods": []}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)
    try:
        payload = _call("wb_call_method", {
            "operation_id": stale.operation_id,
            "body": {"filterNmID": 392023986, "limit": 1, "offset": 0},
            "cabinet": "",
        })
    finally:
        wb.catalog.remove(stale.operation_id)

    assert payload["ok"] is True
    assert payload["requested_operation_id"] == stale.operation_id
    assert payload["resolved_operation_id"] == "wb_prices_list"
    assert payload["equivalent_proven_contract_resolution"] is True
    assert seen["operation_id"] == "wb_prices_list"
    assert seen["method"] == "GET"
    assert seen["query"]["filterNmID"] == 392023986
    assert seen["json_body"] is None


def test_fetch_all_unknown_stale_stock_id_reuses_unique_proven_contract(monkeypatch):
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
    seen = {}

    async def fake_fetch_all(client, spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen["method"] = spec.method
        seen["base_query"] = kwargs.get("base_query")
        seen["base_body"] = kwargs.get("base_body")
        return {"ok": True, "items": [], "total_fetched": 0, "pages_fetched": 1}

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

    assert payload["ok"] is True
    assert payload["requested_operation_id"] == stale.operation_id
    assert payload["resolved_operation_id"] == "wb_analytics_stocks_wb_warehouses"
    assert payload["equivalent_proven_contract_resolution"] is True
    assert seen["operation_id"] == "wb_analytics_stocks_wb_warehouses"
    assert seen["method"] == "POST"
    assert seen["base_query"] is None
    assert seen["base_body"]["nmIds"] == [392023986]


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

    assert changed is False
    assert resolved.operation_id == "stale"


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
