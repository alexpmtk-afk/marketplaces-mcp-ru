from __future__ import annotations

import asyncio
import json

from core.tools import resolve_catalog_raw_spec
from ozon_mcp import server as ozon
from wb_mcp import server as wb


def _call(server, name: str, args: dict) -> dict:
    result = asyncio.run(server.mcp.call_tool(name, args))
    return json.loads(result[0][0].text)


def test_wb_raw_known_price_resolves_to_catalog_and_uses_normal_client(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        seen.update(kwargs)
        return {"ok": True, "status": 200, "data": {"listGoods": []}}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)
    payload = _call(wb, "wb_call_raw", {
        "method": "GET",
        "host": "discounts-prices-api.wildberries.ru",
        "path": "/api/v2/list/goods/filter",
        "query": {"limit": 1, "offset": 0, "filterNmID": 392023986},
    })

    assert payload["ok"] is True
    assert payload["resolved_operation_id"] == "wb_prices_list"
    assert payload["raw_resolution"] == "catalog_read"
    assert seen["operation_id"] == "wb_prices_list"
    assert seen["query"]["filterNmID"] == 392023986


def test_ozon_raw_known_price_resolves_without_host_when_unambiguous(monkeypatch):
    seen = {}

    async def fake_call_spec(spec, **kwargs):
        seen["operation_id"] = spec.operation_id
        return {"ok": True, "status": 200, "data": {"items": []}}

    monkeypatch.setattr(ozon.client, "call_spec", fake_call_spec)
    payload = _call(ozon, "ozon_call_raw", {
        "method": "POST",
        "path": "/v5/product/info/prices",
        "body": {"filter": {"visibility": "ALL"}, "limit": 1},
    })

    assert payload["ok"] is True
    assert payload["resolved_operation_id"] == "ozon_prices_get"
    assert seen["operation_id"] == "ozon_prices_get"


def test_unknown_raw_endpoint_remains_fail_closed(monkeypatch):
    called = False

    async def fake_call_spec(spec, **kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(wb.client, "call_spec", fake_call_spec)
    payload = _call(wb, "wb_call_raw", {
        "method": "GET",
        "host": "discounts-prices-api.wildberries.ru",
        "path": "/unknown/raw/path",
    })

    assert called is False
    assert payload["code"] == "RATE_LIMIT_RULE_UNPROVEN"
    assert payload["raw_resolution"] == "not_catalogued"


def test_known_mutating_raw_endpoint_is_not_auto_approved():
    spec, reason = resolve_catalog_raw_spec(
        wb.catalog,
        method="POST",
        host="discounts-prices-api.wildberries.ru",
        path="/api/v2/upload/task",
    )
    assert spec is None
    assert reason == "unsafe"


def test_unproven_catalog_read_is_not_auto_approved():
    spec = wb.catalog.get("wb_content_categories")
    assert spec is not None
    original = spec.quota_proven
    original_service = spec.service_quota_proven
    try:
        spec.quota_proven = False
        spec.service_quota_proven = False
        resolved, reason = resolve_catalog_raw_spec(
            wb.catalog,
            method=spec.method,
            host=spec.host,
            path=spec.path,
        )
        assert resolved is None
        assert reason == "quota_unproven"
    finally:
        spec.quota_proven = original
        spec.service_quota_proven = original_service
