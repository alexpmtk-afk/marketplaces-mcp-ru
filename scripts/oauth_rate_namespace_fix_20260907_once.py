#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "core" / "client.py"
TESTS = ROOT / "tests" / "test_perf_oauth.py"

client = CLIENT.read_text(encoding="utf-8")
old_rules = '''        rules = build_rules(\n            service=cfg.name, cabinet_key=key, host=cfg.token_url,\n            operation_id="oauth_token",\n        )\n'''
new_rules = '''        # Token issuance is a separate HTTP channel from marketplace API calls.\n        # Keep it under the same service rate policy, but isolate its reservation\n        # namespace so a successful token refresh cannot consume the immediately\n        # following API request slot. This remains fail-fast: no future slot is\n        # reserved when the token channel is busy.\n        rules = build_rules(\n            service=cfg.name, cabinet_key=f"{key}:oauth", host=cfg.token_url,\n            operation_id="oauth_token",\n        )\n'''
if old_rules not in client:
    raise RuntimeError("OAuth build_rules block not found")
client = client.replace(old_rules, new_rules, 1)
CLIENT.write_text(client, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
anchor = '''def _creds(monkeypatch):\n    monkeypatch.setenv("OZON_PERF_CLIENT_ID", "cid-123")\n    monkeypatch.setenv("OZON_PERF_CLIENT_SECRET", "secret-xyz")\n\n\n'''
helper = '''def _creds(monkeypatch):\n    monkeypatch.setenv("OZON_PERF_CLIENT_ID", "cid-123")\n    monkeypatch.setenv("OZON_PERF_CLIENT_SECRET", "secret-xyz")\n\n\nclass _ImmediateRateController:\n    """Deterministic limiter for OAuth contract tests; records rule namespaces."""\n\n    def __init__(self):\n        self.rule_sets = []\n        self.defer_calls = []\n\n    async def try_acquire(self, rules):\n        self.rule_sets.append(tuple(rule.key for rule in rules))\n        return True, 0.0\n\n    async def acquire(self, rules):\n        raise AssertionError("interactive client must not call queue-reserving acquire()")\n\n    async def defer(self, rules, seconds):\n        self.defer_calls.append(seconds)\n\n\n'''
if anchor not in tests:
    raise RuntimeError("OAuth test helper anchor not found")
tests = tests.replace(anchor, helper, 1)
tests = tests.replace(
    'MarketplaceClient(_make_config())',
    'MarketplaceClient(_make_config(), rate_controller=_ImmediateRateController())',
)
tests = tests.replace(
    'client = MarketplaceClient(cfg)\n    r = asyncio.run(client.request("GET", "api-seller.ozon.ru", "/v1/x"))',
    'client = MarketplaceClient(cfg, rate_controller=_ImmediateRateController())\n    r = asyncio.run(client.request("GET", "api-seller.ozon.ru", "/v1/x"))',
)

extra = '''\n\ndef test_oauth_token_and_api_use_separate_rate_limit_namespaces(monkeypatch):\n    _creds(monkeypatch)\n    rec = _Recorder()\n    _patch_async_client(monkeypatch, rec)\n    limiter = _ImmediateRateController()\n    client = MarketplaceClient(_make_config(), rate_controller=limiter)\n\n    result = asyncio.run(client.request("GET", API_HOST, API_PATH))\n\n    assert result["ok"] is True\n    assert len(limiter.rule_sets) == 2\n    token_rules, api_rules = map(set, limiter.rule_sets)\n    assert token_rules.isdisjoint(api_rules), (token_rules, api_rules)\n    assert any(key.endswith(":oauth:global") for key in token_rules)\n    assert all(":oauth:" not in key for key in api_rules)\n'''
if "test_oauth_token_and_api_use_separate_rate_limit_namespaces" not in tests:
    tests += extra
TESTS.write_text(tests, encoding="utf-8")

# Hard guarantees for the production path.
updated = CLIENT.read_text(encoding="utf-8")
if 'cabinet_key=f"{key}:oauth"' not in updated:
    raise RuntimeError("OAuth rate namespace was not isolated")
if ".rate_controller.acquire(" in updated:
    raise RuntimeError("queue-reserving acquire() returned to interactive client")
