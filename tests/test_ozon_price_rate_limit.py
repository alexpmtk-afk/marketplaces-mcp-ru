"""Ozon typed price updates use the documented per-product quota scope."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

from ozon_mcp import server


class FakeClient:
    def __init__(self):
        self.kwargs = None

    async def request(self, method, host, path, **kwargs):
        self.kwargs = {"method": method, "host": host, "path": path, **kwargs}
        return {"ok": True, "status": 200, "data": {"result": []}}


def test_ozon_set_price_has_hashed_per_product_quota_scope():
    fake = FakeClient()
    offer_id = "seller-article-42"
    with patch.object(server, "client", fake):
        result = json.loads(asyncio.run(server.ozon_set_price(
            offer_id, "1499", confirm_write=True,
        )))

    assert result["ok"] is True
    assert fake.kwargs["rate_limit"] == "10 req/hour"
    assert fake.kwargs["rate_scope"].startswith("seller:price-product:")
    assert offer_id not in fake.kwargs["rate_scope"]
