import base64
import json
from types import SimpleNamespace

from core.client import MarketplaceClient
from core.rate_limit import build_rules
from core.registry import Catalog
from core.tools import has_proven_quota


def _wb_token(sid: str, marker: str) -> str:
    raw = base64.urlsafe_b64encode(json.dumps({"sid": sid, "acc": 3, "id": marker}).encode()).decode().rstrip("=")
    return f"header.{raw}.signature"


def test_wb_quota_key_survives_token_rotation():
    cfg = SimpleNamespace(name="wb")
    assert MarketplaceClient._quota_key(cfg, {"token": _wb_token("seller-a", "old")}) == MarketplaceClient._quota_key(cfg, {"token": _wb_token("seller-a", "new")})
    assert MarketplaceClient._quota_key(cfg, {"token": _wb_token("seller-a", "old")}) != MarketplaceClient._quota_key(cfg, {"token": _wb_token("seller-b", "new")})


def test_ozon_quota_key_uses_client_id_not_api_key():
    cfg = SimpleNamespace(name="ozon")
    assert MarketplaceClient._quota_key(cfg, {"client_id": "account-a", "api_key": "old"}) == MarketplaceClient._quota_key(cfg, {"client_id": "account-a", "api_key": "new"})
    assert MarketplaceClient._quota_key(cfg, {"client_id": "account-a", "api_key": "old"}) != MarketplaceClient._quota_key(cfg, {"client_id": "account-b", "api_key": "old"})


def test_wb_has_no_arbitrary_global_rule_but_keeps_documented_method_rule():
    rules = build_rules(service="wb", cabinet_key="stable", host="statistics-api.wildberries.ru", catalog_rate_limit="1 req/min")
    assert len(rules) == 1
    assert ":catalog:" in rules[0].key


def test_generic_catalog_requires_explicit_proven_marker():
    catalog = Catalog.from_yaml("wb_mcp/endpoints.yaml")
    assert has_proven_quota(catalog.get("wb_stats_sales"))
    assert not has_proven_quota(catalog.get("wb_get_api_feedbacks"))

def test_malformed_wb_token_fails_closed_with_a_safe_error():
    cfg = SimpleNamespace(name="wb")
    try:
        MarketplaceClient._quota_key(cfg, {"token": "not-a-jwt"})
    except ValueError as exc:
        assert "quota identity" in str(exc)
    else:
        raise AssertionError("malformed token must not produce a quota key")
