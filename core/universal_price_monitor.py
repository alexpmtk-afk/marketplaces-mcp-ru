"""Read-only MCP projection for the independent HOME universal buyer-price monitor.

This module deliberately never reads legacy card_monitor:v1 keys and never
launches a browser, changes schedules, or writes to Google Sheets. A trusted,
separately approved HOME->REMOTE publisher does not exist yet; until it does,
these tools report NOT_CONNECTED rather than inventing live observations.
"""
from __future__ import annotations

import json
import math
from typing import Any, Mapping

from .rate_limit import redis_connection_kwargs, redis_url_from_env

PREFIX = "universal-price-monitor:v1"
JOB_INDEX = f"{PREFIX}:jobs"
PUBLISHER_STATUS = f"{PREFIX}:publisher-status"
MAX_READ = 100


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def accepted_mirror(value: Any) -> bool:
    """Fail-closed display gate; not a substitute for source evidence audit.

    Producer must attach verifiable exact-model review provenance and proof of
    canonical Google Sheets readback. These flags are producer assertions until
    an independently audited publisher is provisioned.
    """
    if not isinstance(value, Mapping):
        return False
    if value.get("schema") != "UNIVERSAL_PRICE_MONITOR_MIRROR_V1":
        return False
    if not all(isinstance(value.get(k), str) and value[k].strip()
               for k in ("job_id", "observation_id", "query", "sku",
                         "marketplace", "seller", "captured_at", "evidence_id")):
        return False
    if value["marketplace"] not in ("ozon", "wildberries"):
        return False
    if value.get("sheets_readback") != "MATCH":
        return False
    scope = value.get("review_scope")
    if not isinstance(scope, Mapping) or not (
        scope.get("exact_model") is True
        and scope.get("rating_exact_model") is True
        and scope.get("reviews_exact_model") is True
    ):
        return False
    rating = value.get("rating")
    reviews = value.get("review_count")
    price = value.get("price_rub")
    if isinstance(rating, bool) or isinstance(price, bool) or type(reviews) is not int:
        return False
    if reviews < 10:
        return False
    try:
        rating_number = float(rating)
        price_number = float(price)
    except (ValueError, TypeError, OverflowError):
        return False
    if not (math.isfinite(rating_number) and 4.8 <= rating_number <= 5.0
            and math.isfinite(price_number) and price_number > 0):
        return False
    if value["marketplace"] == "wildberries":
        return (value.get("price_basis") == "wildberries_special_payment"
                and str(value.get("payment_condition") or "").startswith("с WB Кошельком;"))
    return (value.get("price_basis") == "ozon_special_payment"
            and str(value.get("payment_condition") or "").casefold().strip() == "с банками")


class UniversalPriceMonitorReadStore:
    def __init__(self, client=None):
        self._redis = client
        self.redis_url = redis_url_from_env() if client is None else "injected"

    async def _client(self):
        if self._redis is None:
            if not self.redis_url:
                return None
            import redis.asyncio as redis_async
            self._redis = redis_async.from_url(
                self.redis_url, decode_responses=True,
                socket_connect_timeout=5, socket_timeout=5,
                **redis_connection_kwargs(self.redis_url))
        return self._redis

    async def status(self) -> dict[str, Any]:
        client = await self._client()
        if client is None:
            return {"status": "NOT_CONNECTED", "reason": "REDIS_NOT_CONFIGURED",
                    "publisher_confirmed": False}
        raw = await client.get(PUBLISHER_STATUS)
        if not raw:
            return {"status": "NOT_CONNECTED", "reason": "UNIVERSAL_HOME_PUBLISHER_NOT_CONFIGURED",
                    "publisher_confirmed": False, "accepted_observations": 0}
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {"status": "INVALID_STATUS", "publisher_confirmed": False}
        if not isinstance(data, dict):
            return {"status": "INVALID_STATUS", "publisher_confirmed": False}
        # Never infer current connectivity merely from a past heartbeat.
        return {"status": "PUBLISHER_REPORTED", "publisher_confirmed": False,
                "last_report": {k: data.get(k) for k in
                                ("source", "last_run_id", "last_completed_at", "last_error")}}

    async def _job_ids(self, requested: str) -> list[str]:
        if requested:
            if not isinstance(requested, str) or len(requested) > 128 or any(
                c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for c in requested
            ):
                raise ValueError("JOB_ID_INVALID")
            return [requested]
        client = await self._client()
        if client is None:
            return []
        values = sorted(str(x) for x in await client.smembers(JOB_INDEX))
        return [x for x in values if x and len(x) <= 128][:MAX_READ]

    async def latest(self, job_id: str = "") -> dict[str, Any]:
        client = await self._client()
        if client is None:
            return {"status": "NOT_CONNECTED", "count": 0, "results": []}
        jobs = await self._job_ids(job_id)
        if not jobs:
            return {"status": "NO_VERIFIED_DATA", "count": 0, "results": []}
        raw = await client.mget([f"{PREFIX}:latest:{x}" for x in jobs])
        results = []
        for expected_id, value in zip(jobs, raw):
            try:
                payload = json.loads(value) if value else None
            except (ValueError, TypeError):
                continue
            # Snapshot may hold up to five distinct approved sellers per job.
            # Legacy single-row staging values remain readable, without expansion.
            offers = payload if isinstance(payload, list) else [payload]
            seen_sellers = set()
            for row in offers:
                if not accepted_mirror(row) or row["job_id"] != expected_id:
                    continue
                seller_key = (row["marketplace"], row["seller"].casefold().strip())
                if seller_key in seen_sellers or len(seen_sellers) >= 5:
                    continue
                seen_sellers.add(seller_key)
                results.append(row)
        return {"status": "PRODUCER_ATTESTED" if results else "NO_VERIFIED_DATA",
                "count": len(results), "results": results}

    async def history(self, job_id: str, limit: int = 50) -> dict[str, Any]:
        ids = await self._job_ids(job_id)
        if len(ids) != 1:
            raise ValueError("JOB_ID_REQUIRED")
        client = await self._client()
        if client is None:
            return {"status": "NOT_CONNECTED", "count": 0, "results": []}
        limit = max(1, min(MAX_READ, int(limit)))
        raw = await client.zrevrange(f"{PREFIX}:history:{ids[0]}", 0, limit - 1)
        rows = []
        for encoded in raw:
            try:
                value = json.loads(encoded)
            except (ValueError, TypeError):
                continue
            if accepted_mirror(value) and value["job_id"] == ids[0]:
                rows.append(value)
        return {"status": "PRODUCER_ATTESTED" if rows else "NO_VERIFIED_DATA",
                "job_id": ids[0], "count": len(rows), "results": rows}


STORE = UniversalPriceMonitorReadStore()


def register_tools(mcp: Any) -> None:
    @mcp.tool(name="universal_price_monitor_status",
              annotations={"title": "HOME universal monitor publisher status",
                           "readOnlyHint": True, "openWorldHint": False})
    async def universal_price_monitor_status() -> str:
        """Inspect connection status; this tool does not start a scan."""
        return _json(await STORE.status())

    @mcp.tool(name="universal_price_monitor_latest",
              annotations={"title": "Verified HOME universal price mirror",
                           "readOnlyHint": True, "openWorldHint": False})
    async def universal_price_monitor_latest(job_id: str = "") -> str:
        """Read only producer-attested, source-scope-verified offer records."""
        return _json(await STORE.latest(job_id))

    @mcp.tool(name="universal_price_monitor_history",
              annotations={"title": "HOME universal price history mirror",
                           "readOnlyHint": True, "openWorldHint": False})
    async def universal_price_monitor_history(job_id: str, limit: int = 50) -> str:
        """Read the safe mirror; Google Sheets remains the canonical history."""
        return _json(await STORE.history(job_id, limit))
