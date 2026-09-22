"""One-time/safe runtime repair helpers for legacy limiter state."""
from __future__ import annotations

import contextlib

from .rate_limit import redis_connection_kwargs, redis_url_from_env


WB_GLOBAL_PATTERN = "marketplace-rate:v1:wb:*:global"


def repair_legacy_wb_global_cooldowns() -> int:
    """Delete every legacy WB global slot from shared Redis.

    The current WB contract is seller + method/group scoped and intentionally
    has no transport-wide global bucket. Any persisted :global WB slot is
    therefore legacy state from the former Yandex deployment (or an older local
    configuration) and must not survive on REMOTE.
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
        for key in client.scan_iter(match=WB_GLOBAL_PATTERN, count=100):
            repaired += int(client.delete(key) or 0)
        return repaired
    finally:
        with contextlib.suppress(Exception):
            client.close()
