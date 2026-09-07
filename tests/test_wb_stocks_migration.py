"""Regression tests for the June 2026 WB stocks API migration."""
from __future__ import annotations

import asyncio
import json

from wb_mcp import server


def test_wb_get_stocks_uses_current_analytics_endpoint(monkeypatch):
    captured = {}

    async def fake_request(method, host, path, **kwargs):
        captured.update({"method": method, "host": host, "path": path, **kwargs})
        return {"ok": True, "status": 200, "data": {"data": {"items": []}}}

    monkeypatch.setattr(server, "_wb_active_token_type", lambda: "unknown")
    monkeypatch.setattr(server.client, "request", fake_request)
    out = json.loads(asyncio.run(server.wb_get_stocks(
        nm_ids=[47254354], chrt_ids=[91663228], limit=25, offset=50,
    )))

    assert out["ok"] is True
    assert captured["method"] == "POST"
    assert captured["host"] == "seller-analytics-api.wildberries.ru"
    assert captured["path"] == "/api/analytics/v1/stocks-report/wb-warehouses"
    assert captured["operation_id"] == "wb_analytics_stocks_wb_warehouses"
    assert captured["rate_limit"] == "3 req/min"
    assert captured["rate_scope"] == "analytics"
    assert captured["json_body"] == {
        "nmIds": [47254354],
        "chrtIds": [91663228],
        "limit": 25,
        "offset": 50,
    }


def test_wb_get_stocks_does_not_rewrite_unrelated_403(monkeypatch):
    async def fake_request(*args, **kwargs):
        return {
            "ok": False,
            "error": "forbidden",
            "error_type": "forbidden",
            "code": 403,
            "message": "upstream denied for another reason",
            "retryable": False,
        }

    monkeypatch.setattr(server, "_wb_active_token_type", lambda: "unknown")
    monkeypatch.setattr(server.client, "request", fake_request)
    out = json.loads(asyncio.run(server.wb_get_stocks(limit=1)))

    assert out["error_type"] == "forbidden"
    assert out["message"] == "upstream denied for another reason"
    assert "required_token_category" not in out


def test_wb_get_stocks_base_token_uses_async_report_fallback(monkeypatch):
    calls = []

    async def fake_request(method, host, path, **kwargs):
        calls.append((method, host, path, kwargs))
        if path == "/api/v1/warehouse_remains":
            return {"ok": True, "status": 200, "data": {"data": {"taskId": "task-1"}}}
        if path.endswith("/status"):
            return {"ok": True, "status": 200, "data": {"data": {"status": "done"}}}
        if path.endswith("/download"):
            return {
                "ok": True,
                "status": 200,
                "data": [
                    {
                        "nmId": 10,
                        "vendorCode": "A",
                        "barcode": "111",
                        "techSize": "M",
                        "warehouses": [
                            {"warehouseName": "Коледино", "quantity": 7},
                            {"warehouseName": "Казань", "quantity": 3},
                        ],
                    },
                    {"nmId": 20, "warehouses": [{"warehouseName": "Тула", "quantity": 99}]},
                ],
            }
        raise AssertionError(path)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(server, "_wb_active_token_type", lambda: "base")
    monkeypatch.setattr(server.client.config, "resolve_creds", lambda: ({"token": "offline-test"}, "test"))
    monkeypatch.setattr(server.client, "request", fake_request)
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)

    out = json.loads(asyncio.run(server.wb_get_stocks(nm_ids=[10], limit=1, offset=1)))

    assert out["ok"] is True
    assert out["source"] == "wb_analytics_warehouse_remains_report"
    assert out["fallback_from"] == "wb_analytics_stocks_wb_warehouses"
    assert out["data"]["data"]["items"] == [
        {
            "nmId": 10,
            "vendorCode": "A",
            "barcode": "111",
            "techSize": "M",
            "warehouseName": "Казань",
            "quantity": 3,
        }
    ]
    assert calls[0][2] == "/api/v1/warehouse_remains"
    assert all(call[2] != "/api/analytics/v1/stocks-report/wb-warehouses" for call in calls)


def test_wb_get_stocks_additional_requirements_triggers_fallback(monkeypatch):
    calls = []

    async def fake_request(method, host, path, **kwargs):
        calls.append(path)
        if path == "/api/analytics/v1/stocks-report/wb-warehouses":
            return {
                "ok": False,
                "code": 403,
                "error_type": "forbidden",
                "message": "WB returned 403: token does not satisfy additional requirements",
            }
        if path == "/api/v1/warehouse_remains":
            return {"ok": True, "status": 200, "data": {"data": {"taskId": "task-2"}}}
        if path.endswith("/status"):
            return {"ok": True, "status": 200, "data": {"data": {"status": "done"}}}
        if path.endswith("/download"):
            return {"ok": True, "status": 204, "data": None}
        raise AssertionError(path)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(server, "_wb_active_token_type", lambda: "unknown")
    monkeypatch.setattr(server.client.config, "resolve_creds", lambda: ({"token": "offline-test"}, "test"))
    monkeypatch.setattr(server.client, "request", fake_request)
    monkeypatch.setattr(server.asyncio, "sleep", no_sleep)

    out = json.loads(asyncio.run(server.wb_get_stocks(limit=1)))
    assert out["ok"] is True
    assert out["data"]["data"]["items"] == []
    assert calls[0] == "/api/analytics/v1/stocks-report/wb-warehouses"
    assert "/api/v1/warehouse_remains" in calls


def test_wb_get_stocks_fallback_rejects_chrt_filter_for_base_token(monkeypatch):
    monkeypatch.setattr(server, "_wb_active_token_type", lambda: "base")
    out = json.loads(asyncio.run(server.wb_get_stocks(nm_ids=[10], chrt_ids=[1], limit=1)))
    assert out["error_type"] == "forbidden"
    assert out["required_token_type"] == ["Personal", "Service"]
    assert "chrt_ids" in out["message"]


def test_wb_get_stocks_rejects_size_filter_without_articles(monkeypatch):
    called = False

    async def fake_request(*args, **kwargs):
        nonlocal called
        called = True
        return {"ok": True, "status": 200, "data": {}}

    monkeypatch.setattr(server.client, "request", fake_request)
    out = json.loads(asyncio.run(server.wb_get_stocks(chrt_ids=[1])))

    assert out["error"] == "invalid_params"
    assert called is False
