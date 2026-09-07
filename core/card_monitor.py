"""Shared public-card monitor storage and MCP read tools.

Windows User Node posts normalized browser observations to the remote server.
The server stores both the latest attempt and the latest validated PASS in the
same production Redis/Valkey already required by SERVER-001.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterable

from .rate_limit import redis_connection_kwargs, redis_url_from_env

PREFIX = "marketplace-card-monitor:v1"
INDEX_KEY = f"{PREFIX}:targets"
LAST_BATCH_KEY = f"{PREFIX}:batch:last"
MAX_HISTORY = 5000


def _j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _epoch(value: Any) -> float:
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def _target_key(kind: str, target_id: str) -> str:
    return f"{PREFIX}:{kind}:{target_id}"


class CardMonitorStore:
    def __init__(self) -> None:
        self.redis_url = redis_url_from_env()
        self._redis = None

    async def _client(self):
        if not self.redis_url:
            raise RuntimeError("shared Redis/Valkey is not configured")
        if self._redis is None:
            try:
                import redis.asyncio as redis_async
            except ImportError as exc:
                raise RuntimeError("package 'redis' is not installed") from exc
            self._redis = redis_async.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                **redis_connection_kwargs(self.redis_url),
            )
        return self._redis

    async def ingest_batch(self, batch: dict[str, Any]) -> dict[str, Any]:
        if batch.get("schema") != "MARKETPLACE_CARD_BATCH_V1":
            raise ValueError("unsupported batch schema")
        results = batch.get("results")
        if not isinstance(results, list):
            raise ValueError("results must be a list")
        if len(results) > 100:
            raise ValueError("too many results; max 100")

        client = await self._client()
        accepted = 0
        pass_count = 0
        pipe = client.pipeline(transaction=True)
        received_at = _now()

        compact_batch = {
            "schema": batch.get("schema"),
            "run_id": batch.get("run_id"),
            "started_at": batch.get("started_at"),
            "finished_at": batch.get("finished_at"),
            "shopper": batch.get("shopper") or {},
            "status": batch.get("status"),
            "cards_total": batch.get("cards_total"),
            "cards_pass": batch.get("cards_pass"),
            "cards_fail": batch.get("cards_fail"),
            "received_at": received_at,
        }
        pipe.set(LAST_BATCH_KEY, _j(compact_batch))

        for item in results:
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("id") or "").strip()
            marketplace = str(item.get("marketplace") or "").strip().lower()
            if not target_id or marketplace not in {"ozon", "wildberries"}:
                continue

            observation = {
                "id": target_id,
                "marketplace": marketplace,
                "brand": item.get("brand"),
                "model": item.get("model"),
                "size": item.get("size"),
                "studded": item.get("studded"),
                "status": item.get("status"),
                "sku": item.get("sku"),
                "url": item.get("final_url") or item.get("target_url"),
                "name": item.get("name"),
                "seller": item.get("seller"),
                "availability": item.get("availability"),
                "region_ok": item.get("region_ok"),
                "price": item.get("price") or {},
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at") or received_at,
                "run_id": batch.get("run_id"),
                "received_at": received_at,
            }
            encoded = _j(observation)
            score = _epoch(observation["finished_at"])
            unique_member = _j({"t": score, "run": batch.get("run_id"), "o": observation})

            pipe.sadd(INDEX_KEY, target_id)
            pipe.set(_target_key("latest-attempt", target_id), encoded)
            pipe.zadd(_target_key("history", target_id), {unique_member: score})
            pipe.zremrangebyrank(_target_key("history", target_id), 0, -(MAX_HISTORY + 1))
            if observation["status"] == "CARD_PASS":
                pipe.set(_target_key("latest-pass", target_id), encoded)
                pass_count += 1
            accepted += 1

        await pipe.execute()
        return {
            "ok": True,
            "run_id": batch.get("run_id"),
            "accepted": accepted,
            "card_pass": pass_count,
            "received_at": received_at,
        }

    async def last_batch(self) -> dict[str, Any] | None:
        client = await self._client()
        raw = await client.get(LAST_BATCH_KEY)
        return json.loads(raw) if raw else None

    async def target_ids(self) -> list[str]:
        client = await self._client()
        return sorted(str(x) for x in await client.smembers(INDEX_KEY))

    async def latest(self, target_ids: Iterable[str] | None = None, *, only_pass: bool = True) -> list[dict[str, Any]]:
        client = await self._client()
        ids = list(target_ids or await self.target_ids())
        if not ids:
            return []
        kind = "latest-pass" if only_pass else "latest-attempt"
        raws = await client.mget([_target_key(kind, target_id) for target_id in ids])
        out = []
        for target_id, raw in zip(ids, raws):
            if raw:
                item = json.loads(raw)
                item.setdefault("id", target_id)
                out.append(item)
        return out

    async def history(self, target_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        target_id = str(target_id or "").strip()
        if not target_id:
            raise ValueError("target_id is required")
        limit = max(1, min(1000, int(limit)))
        client = await self._client()
        rows = await client.zrevrange(_target_key("history", target_id), 0, limit - 1)
        out: list[dict[str, Any]] = []
        for raw in rows:
            payload = json.loads(raw)
            observation = payload.get("o") if isinstance(payload, dict) else None
            if isinstance(observation, dict):
                out.append(observation)
        return out


STORE = CardMonitorStore()


def register_tools(combined: Any) -> None:
    @combined.tool(
        name="card_monitor_status",
        annotations={"title": "Marketplace card monitor status", "readOnlyHint": True, "openWorldHint": False},
    )
    async def card_monitor_status() -> str:
        """Return the latest browser-card collection batch status."""
        batch = await STORE.last_batch()
        targets = await STORE.target_ids()
        return _pretty({"batch": batch, "known_targets": targets, "known_target_count": len(targets)})

    @combined.tool(
        name="card_monitor_get_latest",
        annotations={"title": "Latest marketplace card observations", "readOnlyHint": True, "openWorldHint": False},
    )
    async def card_monitor_get_latest(
        target_ids: list[str] | None = None,
        marketplace: str = "",
        only_pass: bool = True,
    ) -> str:
        """Get latest public-card observations from Ozon/Wildberries.

        Args:
            target_ids: optional monitor target IDs. Empty means every known target.
            marketplace: optional 'ozon' or 'wildberries' filter.
            only_pass: when true, never return a failed attempt instead of the last validated price.
        """
        rows = await STORE.latest(target_ids, only_pass=bool(only_pass))
        marketplace = marketplace.strip().lower()
        if marketplace:
            if marketplace not in {"ozon", "wildberries"}:
                raise ValueError("marketplace must be ozon or wildberries")
            rows = [row for row in rows if row.get("marketplace") == marketplace]
        return _pretty({"count": len(rows), "only_pass": bool(only_pass), "results": rows})

    @combined.tool(
        name="card_monitor_get_history",
        annotations={"title": "Marketplace card price history", "readOnlyHint": True, "openWorldHint": False},
    )
    async def card_monitor_get_history(target_id: str, limit: int = 100, only_pass: bool = True) -> str:
        """Get newest-first history for one monitored card."""
        rows = await STORE.history(target_id, limit=limit)
        if only_pass:
            rows = [row for row in rows if row.get("status") == "CARD_PASS"]
        return _pretty({"target_id": target_id, "count": len(rows), "results": rows})

    @combined.tool(
        name="card_monitor_compare_prices",
        annotations={"title": "Compare current marketplace card prices", "readOnlyHint": True, "openWorldHint": False},
    )
    async def card_monitor_compare_prices() -> str:
        """Return latest validated prices grouped by product model and marketplace."""
        rows = await STORE.latest(only_pass=True)
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            product_key = " | ".join(str(row.get(k) or "") for k in ("brand", "model", "size")).strip(" |")
            group = groups.setdefault(product_key, {"product": product_key, "offers": []})
            price = (row.get("price") or {}).get("buyer_price_rub")
            group["offers"].append({
                "marketplace": row.get("marketplace"), "target_id": row.get("id"),
                "price_rub": price, "seller": row.get("seller"),
                "availability": row.get("availability"), "observed_at": row.get("finished_at"),
                "sku": row.get("sku"), "url": row.get("url"),
            })
        for group in groups.values():
            group["offers"].sort(key=lambda x: (x.get("price_rub") is None, x.get("price_rub") or 0))
        return _pretty({"count": len(groups), "products": list(groups.values())})
