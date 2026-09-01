"""Offline unit tests for the compact WB orders summary tool."""
from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from wb_mcp import server


class FakeStore:
    def resolve_named(self, service, fields, env_map, name):
        if service == "wb" and name == "DTE":
            return {"token": "very-secret-wb-token"}, "DTE"
        return {}, ""


class FakeRateController:
    def __init__(self, *, cached=None, reserved=True, retry_after=0):
        self.cached = cached
        self.reserved = reserved
        self.retry_after = retry_after
        self.cache_set_calls = []
        self.cache_get_key = ""

    async def cache_get(self, key):
        self.cache_get_key = key
        return self.cached

    async def cache_set(self, key, value, ttl):
        self.cache_set_calls.append((key, value, ttl))

    async def try_acquire(self, rules):
        return self.reserved, self.retry_after


class FakeClient:
    def __init__(self, data, controller):
        self.config = SimpleNamespace(
            fields=["token"], env_map={"token": "WB_API_TOKEN"}, store=FakeStore()
        )
        self.rate_controller = controller
        self.data = data
        self.calls = []

    @staticmethod
    def _creds_key(config, creds):
        return "cabinet-hash"

    async def call_spec(self, spec, **kwargs):
        self.calls.append((spec.operation_id, kwargs))
        return {"ok": True, "status": 200, "data": self.data}


def call_tool(data, *, cached=None, reserved=True, retry_after=0):
    controller = FakeRateController(cached=cached, reserved=reserved, retry_after=retry_after)
    fake = FakeClient(data, controller)
    with patch.object(server, "client", fake):
        raw = asyncio.run(server.wb_get_orders_summary("DTE", "2026-08-31", "2026-08-31"))
    return json.loads(raw), fake, controller


class OrdersSummaryTests(unittest.TestCase):
    def test_one_day_counts_non_cancelled_rows_and_sums_finished_price(self):
        result, fake, controller = call_tool([
            {"date": "2026-08-31T10:00:00", "isCancel": False, "finishedPrice": 100.5},
            {"date": "2026-08-31T12:00:00", "isCancel": False, "finishedPrice": "99.5"},
            {"date": "2026-08-31T13:00:00", "isCancel": True, "finishedPrice": 1000},
            {"date": "2026-09-01T00:00:00", "isCancel": False, "finishedPrice": 999},
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["orders_count"], 2)
        self.assertEqual(result["orders_amount"], 200.0)
        self.assertEqual(result["cancelled_orders_excluded"], 1)
        self.assertEqual(result["source"], "wb_stats_orders")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1]["query"], {"dateFrom": "2026-08-31", "flag": 1})
        self.assertEqual(len(controller.cache_set_calls), 1)

    def test_empty_result_is_a_zero_summary(self):
        result, fake, _ = call_tool([])
        self.assertTrue(result["ok"])
        self.assertEqual(result["orders_count"], 0)
        self.assertEqual(result["orders_amount"], 0.0)
        self.assertEqual(len(fake.calls), 1)

    def test_cancelled_orders_are_excluded_from_count_and_amount(self):
        result, _, _ = call_tool([{"date": "2026-08-31", "isCancel": True, "finishedPrice": 400}])
        self.assertEqual(result["orders_count"], 0)
        self.assertEqual(result["orders_amount"], 0.0)
        self.assertEqual(result["cancelled_orders_excluded"], 1)

    def test_busy_rate_limit_returns_retry_without_http_call(self):
        result, fake, _ = call_tool([], reserved=False, retry_after=59.1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "rate_limit_busy")
        self.assertEqual(result["retry_after_sec"], 60)
        self.assertEqual(fake.calls, [])

    def test_multi_day_period_is_refused_without_http_call(self):
        controller = FakeRateController()
        fake = FakeClient([], controller)
        with patch.object(server, "client", fake):
            raw = asyncio.run(server.wb_get_orders_summary(
                "DTE", "2026-08-30", "2026-08-31"
            ))
        result = json.loads(raw)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_params")
        self.assertEqual(fake.calls, [])

    def test_cache_hit_skips_http_call(self):
        cached = json.dumps({"ok": True, "seller": "DTE", "orders_count": 7,
                             "orders_amount": 900.0, "source": "wb_stats_orders"})
        result, fake, _ = call_tool([], cached=cached)
        self.assertEqual(result["cache"], "hit")
        self.assertEqual(result["orders_count"], 7)
        self.assertEqual(fake.calls, [])

    def test_token_is_never_exposed_in_response_or_cache_key(self):
        result, _, controller = call_tool([])
        encoded = json.dumps(result)
        self.assertNotIn("very-secret-wb-token", encoded)
        self.assertNotIn("very-secret-wb-token", controller.cache_get_key)


if __name__ == "__main__":
    unittest.main()
