#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "core" / "client.py"
TESTS = ROOT / "tests" / "test_client_hardening.py"

client = CLIENT.read_text(encoding="utf-8")

old_token = '''        try:\n            await self.rate_controller.acquire(rules)\n            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:\n'''
new_token = '''        try:\n            granted, retry_after = await self.rate_controller.try_acquire(rules)\n            if not granted:\n                return None, make_error(\n                    "rate_limit",\n                    "OAuth token request is locally rate-limited; retry after the "\n                    "reported delay. No future slot was reserved.",\n                    operation_id="oauth_token", endpoint=cfg.token_url,\n                    retryable=True, retry_after_seconds=retry_after,\n                )\n            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as client:\n'''
if old_token not in client:
    raise RuntimeError("OAuth acquire block not found")
client = client.replace(old_token, new_token, 1)

old_request = '''            if not rate_limit_preacquired:\n                try:\n                    await self.rate_controller.acquire(rules)\n                except RateLimitUnavailable as exc:\n                    return make_error(\n                        "rate_limit",\n                        f"Global request controller unavailable; HTTP request was "\n                        f"not sent: {exc}",\n                        operation_id=operation_id,\n                        endpoint=path,\n                        retryable=True,\n                    )\n'''
new_request = '''            if not rate_limit_preacquired:\n                try:\n                    granted, retry_after = await self.rate_controller.try_acquire(rules)\n                except RateLimitUnavailable as exc:\n                    return make_error(\n                        "rate_limit",\n                        f"Global request controller unavailable; HTTP request was "\n                        f"not sent: {exc}",\n                        operation_id=operation_id,\n                        endpoint=path,\n                        retryable=True,\n                    )\n                if not granted:\n                    return make_error(\n                        "rate_limit",\n                        "Request is locally rate-limited; retry after the reported "\n                        "delay. No future slot was reserved and the HTTP request was not sent.",\n                        operation_id=operation_id,\n                        endpoint=path,\n                        retryable=True,\n                        retry_after_seconds=retry_after,\n                    )\n'''
if old_request not in client:
    raise RuntimeError("request acquire block not found")
client = client.replace(old_request, new_request, 1)

old_429 = '''            if resp.status_code == 429 and retry_on_429 and attempt < MAX_RETRIES:\n                delay = min(_marketplace_retry_delay(resp, attempt), 3600.0)\n                try:\n                    await self.rate_controller.defer(rules, delay)\n                except RateLimitUnavailable as exc:\n                    return make_error(\n                        "rate_limit",\n                        f"Marketplace returned 429 and the shared cooldown could "\n                        f"not be stored: {exc}",\n                        code=429, operation_id=operation_id, endpoint=path,\n                        retryable=True, retry_after_seconds=delay,\n                    )\n                attempt += 1\n                continue\n'''
new_429 = '''            if resp.status_code == 429 and retry_on_429:\n                delay = min(_marketplace_retry_delay(resp, attempt), 3600.0)\n                try:\n                    await self.rate_controller.defer(rules, delay)\n                except RateLimitUnavailable as exc:\n                    return make_error(\n                        "rate_limit",\n                        f"Marketplace returned 429 and the shared cooldown could "\n                        f"not be stored: {exc}",\n                        code=429, operation_id=operation_id, endpoint=path,\n                        retryable=True, retry_after_seconds=delay,\n                    )\n                return make_error(\n                    "rate_limit",\n                    "Marketplace returned 429. Shared cooldown was stored; retry "\n                    "after the reported delay instead of waiting inside this MCP call.",\n                    code=429, operation_id=operation_id, endpoint=path,\n                    retryable=True, retry_after_seconds=delay,\n                    details=_capped_details(resp),\n                )\n'''
if old_429 not in client:
    raise RuntimeError("429 retry block not found")
client = client.replace(old_429, new_429, 1)

if ".rate_controller.acquire(" in client:
    raise RuntimeError("interactive client still calls queue-reserving acquire()")
CLIENT.write_text(client, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
anchor = '''def _no_sleep(monkeypatch):\n    async def instant(_):\n        return None\n    monkeypatch.setattr("core.client.asyncio.sleep", instant)\n\n\n'''
helpers = '''def _no_sleep(monkeypatch):\n    async def instant(_):\n        return None\n    monkeypatch.setattr("core.client.asyncio.sleep", instant)\n\n\nclass _ImmediateRateController:\n    def __init__(self):\n        self.defer_calls = []\n\n    async def try_acquire(self, rules):\n        return True, 0.0\n\n    async def acquire(self, rules):\n        raise AssertionError("interactive client must not call queue-reserving acquire()")\n\n    async def defer(self, rules, seconds):\n        self.defer_calls.append(seconds)\n\n\nclass _RejectingRateController(_ImmediateRateController):\n    def __init__(self, retry_after=2.5):\n        super().__init__()\n        self.retry_after = retry_after\n        self.try_calls = 0\n\n    async def try_acquire(self, rules):\n        self.try_calls += 1\n        return False, self.retry_after\n\n\n'''
if anchor not in tests:
    raise RuntimeError("test helper anchor not found")
tests = tests.replace(anchor, helpers, 1)

# Make the existing HTTP-hardening tests deterministic and independent of the persistent limiter DB.
tests = tests.replace('MarketplaceClient(_static_config())', 'MarketplaceClient(_static_config(), rate_controller=_ImmediateRateController())')
tests = tests.replace('MarketplaceClient(_oauth_config())', 'MarketplaceClient(_oauth_config(), rate_controller=_ImmediateRateController())')

old_429_test = '''def test_429_with_garbage_retry_after_falls_back_to_backoff(monkeypatch):\n    calls = {"n": 0}\n\n    def handler(request):\n        calls["n"] += 1\n        if calls["n"] == 1:\n            return httpx.Response(429, headers={"Retry-After": "later"},\n                                  json={"error": "slow down"})\n        return httpx.Response(200, json={"ok": True})\n\n    _route(monkeypatch, handler)\n    _no_sleep(monkeypatch)\n    monkeypatch.setenv("OZON_CLIENT_ID", "100")\n    monkeypatch.setenv("OZON_API_KEY", "secret")\n    client = MarketplaceClient(_static_config(), rate_controller=_ImmediateRateController())\n    r = asyncio.run(client.request("GET", "api-seller.ozon.ru", "/x"))\n    assert r["ok"] is True\n    assert calls["n"] == 2\n'''
new_429_test = '''def test_429_with_garbage_retry_after_returns_fast_retry_signal(monkeypatch):\n    calls = {"n": 0}\n\n    def handler(request):\n        calls["n"] += 1\n        return httpx.Response(429, headers={"Retry-After": "later"},\n                              json={"error": "slow down"})\n\n    _route(monkeypatch, handler)\n    monkeypatch.setenv("OZON_CLIENT_ID", "100")\n    monkeypatch.setenv("OZON_API_KEY", "secret")\n    limiter = _ImmediateRateController()\n    client = MarketplaceClient(_static_config(), rate_controller=limiter)\n    r = asyncio.run(client.request("GET", "api-seller.ozon.ru", "/x"))\n    assert r["ok"] is False\n    assert r["error_type"] == "rate_limit"\n    assert r["code"] == 429\n    assert r["retryable"] is True\n    assert r["retry_after_seconds"] == pytest.approx(1.5)\n    assert limiter.defer_calls == [pytest.approx(1.5)]\n    assert calls["n"] == 1, "429 must not make the interactive MCP call sleep and resend"\n'''
if old_429_test not in tests:
    raise RuntimeError("old 429 test not found after controller normalization")
tests = tests.replace(old_429_test, new_429_test, 1)

extra = '''\n\n# --- interactive limiter must fail fast without queue debt -------------------\n\ndef test_busy_local_rate_slot_returns_retry_after_without_http(monkeypatch):\n    hits = {"n": 0}\n\n    def handler(request):\n        hits["n"] += 1\n        return httpx.Response(200, json={"unexpected": True})\n\n    _route(monkeypatch, handler)\n    limiter = _RejectingRateController(3.25)\n    client = MarketplaceClient(_static_config(), rate_controller=limiter)\n    r = asyncio.run(client.request(\n        "GET", "api-seller.ozon.ru", "/v1/x",\n        creds_override={"client_id": "100", "api_key": "k"},\n    ))\n    assert r["ok"] is False\n    assert r["error_type"] == "rate_limit"\n    assert r["retryable"] is True\n    assert r["retry_after_seconds"] == pytest.approx(3.25)\n    assert limiter.try_calls == 1\n    assert hits["n"] == 0\n\n\ndef test_busy_oauth_token_slot_returns_retry_after_without_http(monkeypatch):\n    hits = {"n": 0}\n\n    def handler(request):\n        hits["n"] += 1\n        return httpx.Response(200, json={"access_token": "unexpected", "expires_in": 1800})\n\n    _route(monkeypatch, handler)\n    limiter = _RejectingRateController(4.5)\n    client = MarketplaceClient(_oauth_config(), rate_controller=limiter)\n    r = asyncio.run(client.request(\n        "GET", "api-performance.ozon.ru", "/x",\n        creds_override={"client_id": "A", "client_secret": "s"},\n    ))\n    assert r["ok"] is False\n    assert r["error_type"] == "rate_limit"\n    assert r["operation_id"] == "oauth_token"\n    assert r["retry_after_seconds"] == pytest.approx(4.5)\n    assert hits["n"] == 0\n'''
if "test_busy_local_rate_slot_returns_retry_after_without_http" not in tests:
    tests += extra
TESTS.write_text(tests, encoding="utf-8")

print("interactive rate-limit fix staged")
