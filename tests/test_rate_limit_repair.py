from __future__ import annotations

from core.rate_limit_repair import repair_legacy_wb_global_cooldowns


class _FakeRedis:
    def __init__(self, values):
        self.values = dict(values)
        self.closed = False

    def time(self):
        return (1000, 0)

    def scan_iter(self, match, count=100):
        assert match == "marketplace-rate:v1:wb:*:global"
        return iter(list(self.values))

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return 1 if self.values.pop(key, None) is not None else 0

    def close(self):
        self.closed = True


def test_repair_deletes_only_impossible_wb_global_slots(monkeypatch):
    stale = "marketplace-rate:v1:wb:cabinet-a:global"
    legit = "marketplace-rate:v1:wb:cabinet-b:global"
    fake = _FakeRedis({
        stale: str((1000 + 120) * 1000),
        legit: str((1000 + 0.2) * 1000),
    })

    monkeypatch.setenv("MARKETPLACE_MCP_REDIS_URL", "redis://example/0")
    monkeypatch.setattr("redis.Redis.from_url", lambda *a, **k: fake)

    repaired = repair_legacy_wb_global_cooldowns()
    assert repaired == 1
    assert stale not in fake.values
    assert legit in fake.values
    assert fake.closed is True


def test_repair_does_nothing_without_redis(monkeypatch):
    monkeypatch.delenv("MARKETPLACE_MCP_REDIS_URL", raising=False)
    monkeypatch.delenv("MARKETPLACE_MCP_REDIS_HOST", raising=False)
    assert repair_legacy_wb_global_cooldowns() == 0
