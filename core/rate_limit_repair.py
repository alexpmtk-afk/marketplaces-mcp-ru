"""One-time/safe runtime repair helpers for legacy limiter state."""
from __future__ import annotations

import contextlib

from .rate_limit import redis_connection_kwargs, redis_url_from_env


WB_GLOBAL_PATTERN = "marketplace-rate:v1:wb:*:global"
WB_GLOBAL_MAX_LEGIT_WAIT_SECONDS = 5.0


def repair_legacy_wb_global_cooldowns() -> int:
    """Delete only impossible legacy WB global cooldowns.

    WB's transport-wide global pacing interval is sub-second (5 RPS by default).
    A persisted ``:global`` slot many seconds in the future can only be legacy
    contamination from the old upstream-429 defer behaviour. Endpoint/catalog
    quota keys are deliberately untouched.
    """
    url = redis_url_from_env()
    if not url:
        return 0
    from redis import Redis

    client = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        **redis_connection_kwargs(url),
    )
    repaired = 0
    try:
        sec, usec = client.time()
        now = float(sec) + float(usec) / 1_000_000
        for key in client.scan_iter(match=WB_GLOBAL_PATTERN, count=100):
            value = client.get(key)
            if value is None:
                continue
            try:
                next_at = float(value) / 1000.0
            except (TypeError, ValueError):
                continue
            if next_at - now > WB_GLOBAL_MAX_LEGIT_WAIT_SECONDS:
                repaired += int(client.delete(key) or 0)
        return repaired
    finally:
        with contextlib.suppress(Exception):
            client.close()
