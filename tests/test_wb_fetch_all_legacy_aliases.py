from __future__ import annotations

import asyncio
import json

from wb_mcp import server as wb


def _call(name: str, args: dict) -> dict:
    result = asyncio.run(wb.mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_fetch_all_legacy_price_id_resolves_to_canonical_get(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen["method"] = spec.method
        seen["query"] = kwargs.get("query")
        seen["json_body"] = kwargs.get("json_body")
        return {"ok": True, "status": 200, "data": {"data": {"listGoods": []}}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)

    payload = _call("wb_fetch_all", {
        "operation_id": "wb_post_api_list_goods_filter",
        "body": {"filterNmID": 392023986, "limit": 1, "offset": 0},
        "cabinet": "",
        "max_items": 10,
    })

    assert payload["ok"] is True
    assert payload["requested_operation_id"] == "wb_post_api_list_goods_filter"
    assert payload["resolved_operation_id"] == "wb_prices_list"
    assert payload["legacy_alias_resolution"] is True
    assert seen["operation_id"] == "wb_prices_list"
    assert seen["method"] == "GET"
    assert seen["query"]["filterNmID"] == 392023986
    assert seen["json_body"] is None


def test_fetch_all_legacy_price_id_accepts_single_nmids_list(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["query"] = kwargs.get("query")
        return {"ok": True, "status": 200, "data": {"data": {"listGoods": []}}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)

    payload = _call("wb_fetch_all", {
        "operation_id": "wb_post_api_list_goods_filter",
        "body": {"nmIds": [392023986]},
        "max_items": 10,
    })

    assert payload["ok"] is True
    assert seen["query"]["filterNmID"] == 392023986


def test_fetch_all_legacy_price_id_rejects_multi_nmids_without_silent_semantic_change():
    payload = _call("wb_fetch_all", {
        "operation_id": "wb_post_api_list_goods_filter",
        "body": {"nmIds": [392023986, 392023987]},
        "max_items": 10,
    })

    assert payload["ok"] is False
    assert payload["error"] == "invalid_params"
    assert payload["operation_id"] == "wb_post_api_list_goods_filter"


def test_fetch_all_legacy_stock_id_resolves_to_canonical_post(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen["method"] = spec.method
        seen["query"] = kwargs.get("query")
        seen["json_body"] = kwargs.get("json_body")
        return {"ok": True, "status": 200, "data": {"data": {"items": []}}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)

    payload = _call("wb_fetch_all", {
        "operation_id": "wb_post_api_analytics_stocks_report_wb_warehouses",
        "query": {"nmIds": [392023986], "limit": 1000, "offset": 0},
        "max_items": 10,
    })

    assert payload["ok"] is True
    assert payload["requested_operation_id"] == "wb_post_api_analytics_stocks_report_wb_warehouses"
    assert payload["resolved_operation_id"] == "wb_analytics_stocks_wb_warehouses"
    assert payload["legacy_alias_resolution"] is True
    assert seen["operation_id"] == "wb_analytics_stocks_wb_warehouses"
    assert seen["method"] == "POST"
    assert seen["query"] is None
    assert seen["json_body"]["nmIds"] == [392023986]


def test_call_method_legacy_price_id_resolves_too(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen["query"] = kwargs.get("query")
        seen["json_body"] = kwargs.get("json_body")
        return {"ok": True, "status": 200, "data": {"data": {"listGoods": []}}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)

    payload = _call("wb_call_method", {
        "operation_id": "wb_post_api_list_goods_filter",
        "body": {"filterNmID": 392023986},
    })

    assert payload["ok"] is True
    assert payload["resolved_operation_id"] == "wb_prices_list"
    assert seen["operation_id"] == "wb_prices_list"
    assert seen["query"]["filterNmID"] == 392023986
    assert seen["json_body"] is None
