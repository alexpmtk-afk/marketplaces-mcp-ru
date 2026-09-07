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


def test_wb_get_stocks_explains_missing_analytics_scope(monkeypatch):
    async def fake_request(*args, **kwargs):
        return {
            "ok": False,
            "error": "forbidden",
            "error_type": "forbidden",
            "code": 403,
            "message": "upstream denied",
            "retryable": False,
        }

    monkeypatch.setattr(server.client, "request", fake_request)
    out = json.loads(asyncio.run(server.wb_get_stocks(limit=1)))

    assert out["error_type"] == "forbidden"
    assert out["required_token_category"] == "analytics"
    assert "Analytics" in out["message"]
    assert "deprecated Statistics endpoint" in out["message"]


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
