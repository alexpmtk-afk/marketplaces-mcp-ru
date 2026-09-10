"""Rate-limit diagnostics must work even when WB has no global ceiling."""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from core.client import MarketplaceClient, ServiceConfig


def wb_test_token() -> str:
    payload = base64.urlsafe_b64encode(b'{"sid":"seller-a"}').decode().rstrip("=")
    return f"header.{payload}.signature"


class Store:
    def resolve(self, service, fields, env_map):
        if service == "wb":
            return {"token": wb_test_token()}, "test"
        return {"client_id": "client-a", "api_key": "key"}, "test"


class Controller:
    async def snapshot(self, prefix):
        return {"backend": "sqlite", "observed_at_utc": "2026-01-01T00:00:00+00:00", "active_queues": []}


def config(name, fields):
    return ServiceConfig(
        name=name, scheme="https", fields=fields, env_map={},
        build_headers=lambda creds: {}, store=Store(),
    )


def test_wb_rate_limit_status_reports_no_invented_global_ceiling(monkeypatch):
    monkeypatch.delenv("WB_GLOBAL_RPS", raising=False)
    client = MarketplaceClient(config("wb", ["token"]), rate_controller=Controller())
    status = asyncio.run(client.rate_limit_status())
    assert status["ok"] is True
    assert status["configured_global_rps"] is None


def test_ozon_rate_limit_status_reports_documented_client_ceiling(monkeypatch):
    monkeypatch.delenv("OZON_GLOBAL_RPS", raising=False)
    client = MarketplaceClient(config("ozon", ["client_id", "api_key"]), rate_controller=Controller())
    status = asyncio.run(client.rate_limit_status())
    assert status["ok"] is True
    assert status["configured_global_rps"] == 50.0
