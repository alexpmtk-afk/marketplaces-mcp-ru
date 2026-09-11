"""Named-cabinet routing never relies on mutable shared active state."""
from __future__ import annotations

import asyncio
import json

from core.tools import resolve_named_cabinet
from ozon_mcp import server as ozon


class _Store:
    def __init__(self, cabinets):
        self.cabinets = cabinets
        self.calls = []

    def resolve_named(self, service, fields, env_map, name):
        self.calls.append((service, name))
        values = self.cabinets.get(name)
        if values is None:
            return {}, ""
        return {field: values.get(field, "") for field in fields}, name

    def list_cabinets(self, service):
        return {"active": "other", "cabinets": sorted(self.cabinets)}


def test_named_resolution_does_not_read_or_change_active_cabinet():
    store = _Store({"requested": {"client_id": "client-a", "api_key": "key-a"}})

    class Config:
        name = "ozon"
        fields = ["client_id", "api_key"]
        env_map = {}

    Config.store = store

    class Client:
        config = Config()

    creds, error = resolve_named_cabinet(Client(), "requested")

    assert error is None
    assert creds == {"client_id": "client-a", "api_key": "key-a"}
    assert store.calls == [("ozon", "requested")]


def test_named_resolution_rejects_unknown_cabinet_before_provider_call():
    store = _Store({})

    class Config:
        name = "ozon"
        fields = ["client_id", "api_key"]
        env_map = {}

    Config.store = store

    class Client:
        config = Config()

    creds, error = resolve_named_cabinet(Client(), "missing")

    assert creds is None
    assert error["code"] == "CABINET_NOT_CONFIGURED"
    assert error["cabinet"] == "missing"


def test_ozon_revenue_summary_uses_requested_cabinet(monkeypatch):
    store = _Store({
        "ozon_dmitrieva": {"client_id": "client-a", "api_key": "key-a"},
        "other": {"client_id": "client-b", "api_key": "key-b"},
    })
    captured = {}

    async def call_spec(spec, *, json_body=None, creds_override=None, **kwargs):
        captured["operation"] = spec.operation_id
        captured["body"] = json_body
        captured["creds"] = creds_override
        return {"ok": True, "data": {"result": {"totals": [52834]}}}

    monkeypatch.setattr(ozon.client.config, "store", store)
    monkeypatch.setattr(ozon.client, "call_spec", call_spec)

    result = json.loads(asyncio.run(ozon.ozon_get_revenue_summary(
        "ozon_dmitrieva", "2026-09-10"
    )))

    assert result == {
        "ok": True,
        "cabinet": "ozon_dmitrieva",
        "date_from": "2026-09-10",
        "date_to": "2026-09-10",
        "revenue": 52834,
        "currency": "RUB",
        "source": "ozon_analytics_data",
    }
    assert captured["operation"] == "ozon_analytics_data"
    assert captured["body"]["metrics"] == ["revenue"]
    assert captured["creds"] == {"client_id": "client-a", "api_key": "key-a"}
    assert store.calls == [("ozon", "ozon_dmitrieva")]


def test_ozon_quota_scope_status_discloses_only_group_membership(monkeypatch):
    store = _Store({
        "cab_a": {"client_id": "same-client", "api_key": "key-a"},
        "cab_b": {"client_id": "same-client", "api_key": "key-b"},
        "cab_c": {"client_id": "other-client", "api_key": "key-c"},
    })
    monkeypatch.setattr(ozon.client.config, "store", store)

    result = json.loads(asyncio.run(ozon.ozon_quota_scope_status()))

    assert result == {
        "ok": True,
        "quota_scope_groups": [["cab_a", "cab_b"], ["cab_c"]],
        "incomplete_cabinets": [],
    }
    encoded = json.dumps(result)
    assert "same-client" not in encoded
    assert "other-client" not in encoded
    assert "key-a" not in encoded
