"""Process-wide and host-wide marketplace request scheduling.

Every HTTP attempt reserves its send time here before it reaches WB/Ozon.
SQLite coordinates all MCP processes on one host. When shared Redis/Valkey is
configured, atomic Lua scripts coordinate all processes and all replicas.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import quote


_RATE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(?:req(?:uest)?s?)?\s*/\s*"
    r"(?:(\d+(?:\.\d+)?)\s*)?(s|sec|second|m|min|minute|h|hour)s?\s*$",
    re.IGNORECASE,
)
_UNIT_SECONDS = {
    "s": 1.0, "sec": 1.0, "second": 1.0,
    "m": 60.0, "min": 60.0, "minute": 60.0,
    "h": 3600.0, "hour": 3600.0,
}


class RateLimitUnavailable(RuntimeError):
    """The configured shared controller cannot safely reserve a request."""


def redis_url_from_env() -> str:
    """Build a Redis/Valkey URL from deployment components.

    ``MARKETPLACE_MCP_REDIS_URL`` wins for backwards compatibility. Production
    may instead inject host/port/password separately from Lockbox so the secret
    password never has to appear in Terraform source or a GitHub secret URL.
    """
    explicit = os.environ.get("MARKETPLACE_MCP_REDIS_URL", "").strip()
    if explicit:
        return explicit
    host = os.environ.get("MARKETPLACE_MCP_REDIS_HOST", "").strip()
    if not host:
        return ""
    tls = os.environ.get("MARKETPLACE_MCP_REDIS_TLS", "1").strip().lower() in {
        "1", "true", "yes", "on",
    }
    scheme = "rediss" if tls else "redis"
    default_port = "6380" if tls else "6379"
    port = os.environ.get("MARKETPLACE_MCP_REDIS_PORT", default_port).strip() or default_port
    password = os.environ.get("MARKETPLACE_MCP_REDIS_PASSWORD", "")
    auth = f":{quote(password, safe='')}@" if password else ""
    db = os.environ.get("MARKETPLACE_MCP_REDIS_DB", "0").strip() or "0"
    return f"{scheme}://{auth}{host}:{port}/{db}"


def redis_connection_kwargs(url: str) -> dict:
    if not str(url).lower().startswith("rediss://"):
        return {}
    ca = os.environ.get("MARKETPLACE_MCP_REDIS_CA_CERT", "").strip()
    return {"ssl_ca_certs": ca} if ca else {}


def verify_shared_redis() -> None:
    """Fail fast if the production Redis/Valkey backend cannot be reached."""
    url = redis_url_from_env()
    if not url:
        raise RateLimitUnavailable("shared Redis/Valkey is not configured")
    try:
        from redis import Redis
    except ImportError as exc:
        raise RateLimitUnavailable("package 'redis' is not installed") from exc
    client = None
    try:
        client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            **redis_connection_kwargs(url),
        )
        client.ping()
    except Exception as exc:  # noqa: BLE001
        raise RateLimitUnavailable(
            f"shared Redis/Valkey preflight failed: {type(exc).__name__}"
        ) from exc
    finally:
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


@dataclass(frozen=True)
class RateRule:
    key: str
    interval_seconds: float


def parse_rate_limit(value: str) -> Optional[float]:
    match = _RATE_RE.match(value or "")
    if not match:
        return None
    count = float(match.group(1))
    period = float(match.group(2) or 1.0)
    unit = match.group(3).lower()
    if count <= 0 or period <= 0:
        return None
    return period * _UNIT_SECONDS[unit] / count


def _safe_component(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def key_prefix(service: str, cabinet_key: str) -> str:
    return f"marketplace-rate:v1:{service.lower().strip()}:{cabinet_key}"


def configured_global_rps(service: str) -> Optional[float]:
    """Return the documented service-wide ceiling or an explicit override."""
    service = service.lower().strip()
    default_rps = {"wb": None, "ozon": 50.0, "ozon_perf": 10.0}.get(service, 5.0)
    env_name = {
        "wb": "WB_GLOBAL_RPS", "ozon": "OZON_GLOBAL_RPS",
        "ozon_perf": "OZON_PERF_GLOBAL_RPS",
    }.get(service, "MARKETPLACE_GLOBAL_RPS")
    configured = os.environ.get(env_name, "").strip()
    try:
        return max(0.01, float(configured)) if configured else default_rps
    except ValueError:
        return default_rps


def build_rules(*, service: str, cabinet_key: str, host: str,
                operation_id: Optional[str] = None, scope: str = "",
                catalog_rate_limit: str = "") -> list[RateRule]:
    service = service.lower().strip()
    # WB documents quotas per seller account and method/group, not a single
    # marketplace-wide ceiling. Ozon documents a 50 RPS aggregate per Client-Id.
    # An explicit environment value remains an operational override.
    global_rps = configured_global_rps(service)

    prefix = key_prefix(service, cabinet_key)
    rules = [] if global_rps is None else [RateRule(f"{prefix}:global", 1.0 / global_rps)]
    interval = parse_rate_limit(catalog_rate_limit)
    if interval is not None:
        group = "|".join((host.lower(), scope.lower(), catalog_rate_limit.lower()))
        rules.append(RateRule(f"{prefix}:catalog:{_safe_component(group)}", interval))
    elif service == "wb" and not operation_id:
        rules.append(RateRule(f"{prefix}:raw", 1.0))
    return rules


_REDIS_RESERVE_LUA = r"""
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local grant_ms = now_ms
for i, key in ipairs(KEYS) do
  local current = tonumber(redis.call('GET', key) or '0')
  if current > grant_ms then grant_ms = current end
end
for i, key in ipairs(KEYS) do
  local interval_ms = tonumber(ARGV[i])
  local next_ms = grant_ms + interval_ms
  local ttl_ms = math.max(60000, next_ms - now_ms + interval_ms * 10)
  redis.call('SET', key, tostring(next_ms), 'PX', math.floor(ttl_ms))
end
return math.max(0, grant_ms - now_ms)
"""

_REDIS_DEFER_LUA = r"""
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local blocked_until = now_ms + tonumber(ARGV[1])
for i, key in ipairs(KEYS) do
  local current = tonumber(redis.call('GET', key) or '0')
  local target = math.max(current, blocked_until)
  local ttl_ms = math.max(60000, target - now_ms + 60000)
  redis.call('SET', key, tostring(target), 'PX', math.floor(ttl_ms))
end
return blocked_until
"""

_REDIS_TRY_RESERVE_LUA = r"""
local now_parts = redis.call('TIME')
local now_ms = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local next_ms = now_ms
for i, key in ipairs(KEYS) do
  local current = tonumber(redis.call('GET', key) or '0')
  if current > next_ms then next_ms = current end
end
if next_ms > now_ms then return {0, next_ms - now_ms} end
for i, key in ipairs(KEYS) do
  local interval_ms = tonumber(ARGV[i])
  local ttl_ms = math.max(60000, interval_ms * 11)
  redis.call('SET', key, tostring(now_ms + interval_ms), 'PX', math.floor(ttl_ms))
end
return {1, 0}
"""


class GlobalRateController:
    def __init__(self, *, redis_url: Optional[str] = None,
                 sqlite_path: Optional[str | Path] = None):
        self.redis_url = redis_url if redis_url is not None else redis_url_from_env()
        self.require_redis = os.environ.get(
            "MARKETPLACE_REQUIRE_REDIS", "").strip().lower() in {"1", "true", "yes", "on"}
        if self.require_redis and not self.redis_url:
            raise RateLimitUnavailable(
                "MARKETPLACE_REQUIRE_REDIS is enabled, but shared Redis/Valkey is not configured"
            )
        default_db = Path.home() / ".marketplace-mcp" / "rate_limits.sqlite3"
        self.sqlite_path = Path(sqlite_path or os.environ.get(
            "MARKETPLACE_RATE_LIMIT_DB", str(default_db)))
        self._redis = None

    @property
    def backend(self) -> str:
        return "redis" if self.redis_url else "sqlite"

    async def acquire(self, rules: Iterable[RateRule]) -> float:
        normalized = [r for r in rules if r.interval_seconds > 0]
        if not normalized:
            return 0.0
        global_rules = [r for r in normalized if r.key.endswith(":global")]
        specific_rules = [r for r in normalized if not r.key.endswith(":global")]
        stages = [group for group in (specific_rules, global_rules) if group]
        total_delay = 0.0
        for group in stages:
            try:
                delay = await self._reserve_redis(group) if self.redis_url else await asyncio.to_thread(self._reserve_sqlite, group)
            except RateLimitUnavailable:
                raise
            except Exception as exc:
                raise RateLimitUnavailable(
                    f"{self.backend} rate-limit backend unavailable: {type(exc).__name__}: {exc}"
                ) from exc
            if delay > 0:
                await asyncio.sleep(delay)
                total_delay += delay
        return total_delay

    async def try_acquire(self, rules: Iterable[RateRule]) -> tuple[bool, float]:
        """Reserve all slots now, or return the retry delay without queueing.

        This is for interactive paths where sleeping until a marketplace window
        opens would make an MCP request appear hung.
        """
        normalized = [rule for rule in rules if rule.interval_seconds > 0]
        if not normalized:
            return True, 0.0
        try:
            if self.redis_url:
                return await self._try_reserve_redis(normalized)
            return await asyncio.to_thread(self._try_reserve_sqlite, normalized)
        except RateLimitUnavailable:
            raise
        except Exception as exc:
            raise RateLimitUnavailable(
                f"{self.backend} rate-limit backend unavailable: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    async def cache_get(self, key: str) -> Optional[str]:
        """Fetch an optional shared result cache entry without exposing keys."""
        if not self.redis_url:
            return None
        client = await self._redis_client()
        value = await client.get(key)
        return str(value) if value is not None else None

    async def cache_set(self, key: str, value: str, ttl_seconds: int) -> None:
        """Store an optional shared result cache entry with an explicit TTL."""
        if not self.redis_url:
            return
        client = await self._redis_client()
        await client.set(key, value, ex=max(1, int(ttl_seconds)))

    async def defer(self, rules: Iterable[RateRule], seconds: float) -> None:
        normalized = [r for r in rules if r.interval_seconds > 0]
        specific = [r for r in normalized if not r.key.endswith(":global")]
        # Upstream Retry-After belongs to the endpoint/quota bucket, not to the
        # transport-wide pacing bucket. If a specific rule exists, never let one
        # strict marketplace method freeze every other method for the cabinet.
        selected = specific or normalized
        keys = [r.key for r in selected]
        seconds = max(0.0, seconds)
        if not keys or seconds <= 0:
            return
        try:
            if self.redis_url:
                await self._defer_redis(keys, seconds)
            else:
                await asyncio.to_thread(self._defer_sqlite, keys, seconds)
        except Exception as exc:
            raise RateLimitUnavailable(
                f"{self.backend} rate-limit backend unavailable: {type(exc).__name__}: {exc}"
            ) from exc

    async def snapshot(self, prefix: str) -> dict:
        try:
            if self.redis_url:
                rows, now = await self._snapshot_redis(prefix)
            else:
                rows, now = await asyncio.to_thread(self._snapshot_sqlite, prefix)
        except RateLimitUnavailable:
            raise
        except Exception as exc:
            raise RateLimitUnavailable(
                f"{self.backend} rate-limit backend unavailable: {type(exc).__name__}: {exc}"
            ) from exc
        queues = []
        for key, next_at in sorted(rows, key=lambda item: item[0]):
            suffix = key[len(prefix) + 1:] if key.startswith(prefix + ":") else key
            if suffix == "global":
                queue, queue_id = "global", None
            elif suffix.startswith("catalog:"):
                queue, queue_id = "catalog_group", suffix.split(":", 1)[1][:8]
            elif suffix == "raw":
                queue, queue_id = "raw", None
            else:
                queue, queue_id = "other", _safe_component(suffix)[:8]
            item = {
                "queue": queue,
                "next_allowed_at_utc": datetime.fromtimestamp(next_at, tz=timezone.utc).isoformat(),
                "wait_seconds": round(max(0.0, next_at - now), 3),
            }
            if queue_id:
                item["queue_id"] = queue_id
            queues.append(item)
        return {
            "backend": self.backend,
            "observed_at_utc": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "active_queues": queues,
        }

    async def _snapshot_redis(self, prefix: str) -> tuple[list[tuple[str, float]], float]:
        client = await self._redis_client()
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*", count=100)]
        now_parts = await client.time()
        now = float(now_parts[0]) + float(now_parts[1]) / 1_000_000
        if not keys:
            return [], now
        values = await client.mget(keys)
        return [(str(key), float(value) / 1000.0) for key, value in zip(keys, values) if value is not None], now

    def _snapshot_sqlite(self, prefix: str) -> tuple[list[tuple[str, float]], float]:
        conn = self._connect_sqlite()
        try:
            rows = conn.execute(
                "SELECT limiter_key, next_at FROM rate_slots WHERE limiter_key LIKE ?",
                (prefix + ":%",),
            ).fetchall()
            return [(str(key), float(next_at)) for key, next_at in rows], time.time()
        finally:
            conn.close()

    async def _redis_client(self):
        if self._redis is None:
            try:
                import redis.asyncio as redis_async
            except ImportError as exc:
                raise RateLimitUnavailable("Redis backend configured but package 'redis' is not installed") from exc
            self._redis = redis_async.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                **redis_connection_kwargs(self.redis_url),
            )
        return self._redis

    async def _reserve_redis(self, rules: list[RateRule]) -> float:
        client = await self._redis_client()
        keys = [r.key for r in rules]
        intervals_ms = [max(1, round(r.interval_seconds * 1000)) for r in rules]
        delay_ms = await client.eval(_REDIS_RESERVE_LUA, len(keys), *keys, *intervals_ms)
        return float(delay_ms) / 1000.0

    async def _defer_redis(self, keys: list[str], seconds: float) -> None:
        client = await self._redis_client()
        await client.eval(_REDIS_DEFER_LUA, len(keys), *keys, max(1, round(seconds * 1000)))

    async def _try_reserve_redis(self, rules: list[RateRule]) -> tuple[bool, float]:
        client = await self._redis_client()
        result = await client.eval(
            _REDIS_TRY_RESERVE_LUA, len(rules),
            *[rule.key for rule in rules],
            *[max(1, round(rule.interval_seconds * 1000)) for rule in rules],
        )
        return bool(int(result[0])), float(result[1]) / 1000.0

    def _connect_sqlite(self) -> sqlite3.Connection:
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.sqlite_path), timeout=30.0)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("CREATE TABLE IF NOT EXISTS rate_slots (limiter_key TEXT PRIMARY KEY, next_at REAL NOT NULL)")
        return conn

    def _reserve_sqlite(self, rules: list[RateRule]) -> float:
        conn = self._connect_sqlite()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            grant_at = now
            for rule in rules:
                row = conn.execute("SELECT next_at FROM rate_slots WHERE limiter_key = ?", (rule.key,)).fetchone()
                if row:
                    grant_at = max(grant_at, float(row[0]))
            for rule in rules:
                conn.execute(
                    "INSERT INTO rate_slots(limiter_key, next_at) VALUES(?, ?) ON CONFLICT(limiter_key) DO UPDATE SET next_at=excluded.next_at",
                    (rule.key, grant_at + rule.interval_seconds),
                )
            conn.commit()
            return max(0.0, grant_at - now)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _defer_sqlite(self, keys: list[str], seconds: float) -> None:
        conn = self._connect_sqlite()
        try:
            conn.execute("BEGIN IMMEDIATE")
            blocked_until = time.time() + seconds
            for key in keys:
                row = conn.execute("SELECT next_at FROM rate_slots WHERE limiter_key = ?", (key,)).fetchone()
                target = max(blocked_until, float(row[0]) if row else 0.0)
                conn.execute(
                    "INSERT INTO rate_slots(limiter_key, next_at) VALUES(?, ?) ON CONFLICT(limiter_key) DO UPDATE SET next_at=excluded.next_at",
                    (key, target),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _try_reserve_sqlite(self, rules: list[RateRule]) -> tuple[bool, float]:
        conn = self._connect_sqlite()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            rows = [conn.execute(
                "SELECT next_at FROM rate_slots WHERE limiter_key = ?", (rule.key,)
            ).fetchone() for rule in rules]
            next_at = max([now] + [float(row[0]) for row in rows if row])
            if next_at > now:
                conn.rollback()
                return False, next_at - now
            for rule in rules:
                conn.execute(
                    "INSERT INTO rate_slots(limiter_key, next_at) VALUES(?, ?) "
                    "ON CONFLICT(limiter_key) DO UPDATE SET next_at=excluded.next_at",
                    (rule.key, now + rule.interval_seconds),
                )
            conn.commit()
            return True, 0.0
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
