"""Fail-closed read-only MCP projection; synthetic data only, no Redis or Google I/O."""
from __future__ import annotations

import asyncio
import json
from core.universal_price_monitor import (
    PREFIX, PUBLISHER_STATUS, JOB_INDEX, UniversalPriceMonitorReadStore,
    accepted_mirror, register_tools,
)


def specimen(**overrides):
    item = {
        "schema": "UNIVERSAL_PRICE_MONITOR_MIRROR_V1",
        "job_id": "mp-example", "observation_id": "obs-1",
        "query": "Ninja AF140EU", "sku": "3418323101", "marketplace": "ozon",
        "seller": "Example", "captured_at": "2026-10-27T10:00:00+03:00",
        "evidence_id": "sha256-example-only",
        "price_rub": 11318, "price_basis": "ozon_special_payment",
        "payment_condition": "С банками", "rating": 5.0, "review_count": 46,
        "review_scope": {"exact_model": True, "rating_exact_model": True,
                         "reviews_exact_model": True},
        "sheets_readback": "MATCH",
    }
    item.update(overrides)
    return item


class FakeRedis:
    def __init__(self):
        self.kv = {}
        self.sets = {JOB_INDEX: {"mp-example", "mp-other"}}
        self.zsets = {}

    async def get(self, key):
        return self.kv.get(key)

    async def smembers(self, key):
        return self.sets.get(key, set())

    async def mget(self, keys):
        return [self.kv.get(k) for k in keys]

    async def zrevrange(self, key, start, stop):
        return self.zsets.get(key, [])[start:stop + 1]


def test_strict_eligibility():
    assert accepted_mirror(specimen())
    for bad in (
        {"rating": 4.7},
        {"review_count": None, "rating_count": 200},
        {"review_count": 9},
        {"review_scope": {"exact_model": True, "rating_exact_model": False,
                          "reviews_exact_model": True}},
        {"sheets_readback": "PENDING"},
        {"evidence_id": ""},
        {"price_rub": 0},
        {"marketplace": "wildberries", "price_basis": "standard"},
        {"schema": "MARKETPLACE_CARD_BATCH_V1"},
    ):
        assert not accepted_mirror(specimen(**bad))


def test_wb_one_wallet_only():
    assert accepted_mirror(specimen(
        marketplace="wildberries", price_basis="wildberries_special_payment",
        payment_condition="с WB Кошельком; показанная скидка"))
    assert not accepted_mirror(specimen(
        marketplace="wildberries", price_basis="standard",
        payment_condition="без WB Кошелька"))


def test_no_publisher_is_explicitly_not_connected():
    result = asyncio.run(UniversalPriceMonitorReadStore(FakeRedis()).status())
    assert result["status"] == "NOT_CONNECTED"
    assert result["publisher_confirmed"] is False


def test_latest_and_history_do_not_surface_failed_or_other_job_rows():
    db = FakeRedis()
    db.kv[f"{PREFIX}:latest:mp-example"] = json.dumps(specimen())
    db.kv[f"{PREFIX}:latest:mp-other"] = json.dumps(specimen(job_id="mp-other",rating=4.0))
    db.zsets[f"{PREFIX}:history:mp-example"] = [
        json.dumps(specimen()), json.dumps(specimen(rating=4.1)),
        json.dumps(specimen(job_id="mp-other"))
    ]
    store = UniversalPriceMonitorReadStore(db)
    latest = asyncio.run(store.latest())
    assert latest["status"] == "PRODUCER_ATTESTED" and latest["count"] == 1
    history = asyncio.run(store.history("mp-example"))
    assert history["count"] == 1
    assert history["results"][0]["observation_id"] == "obs-1"


def test_invalid_job_id_rejected_before_key_construction():
    db = FakeRedis()
    store = UniversalPriceMonitorReadStore(db)
    try:
        asyncio.run(store.history("../../other"))
    except ValueError as exc:
        assert "JOB_ID_INVALID" in str(exc)
    else:
        raise AssertionError("invalid id accepted")


def test_only_new_prefixed_tools_registered():
    class FakeMcp:
        names = []
        def tool(self, name, annotations):
            self.names.append(name)
            assert annotations["readOnlyHint"] is True
            return lambda fn: fn
    obj = FakeMcp()
    register_tools(obj)
    assert obj.names == [
        "universal_price_monitor_status",
        "universal_price_monitor_latest",
        "universal_price_monitor_history",
    ]
