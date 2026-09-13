from __future__ import annotations

import base64
import json

from core.wb_token_rate_policy import effective_wb_rate_limit, wb_token_type


def _token(acc: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({
        "acc": acc,
        "sid": "seller-id",
    }).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_wb_token_type_decodes_public_acc_claim():
    assert wb_token_type(_token(1)) == "base"
    assert wb_token_type(_token(2)) == "test"
    assert wb_token_type(_token(3)) == "personal"
    assert wb_token_type(_token(4)) == "service"
    assert wb_token_type("not-a-jwt") is None


def test_base_token_gets_documented_slow_statistics_limits():
    token = _token(1)
    assert effective_wb_rate_limit("wb_stats_orders", "1 req/min", token) == "1 req/3h"
    assert effective_wb_rate_limit("wb_stats_sales", "1 req/min", token) == "1 req/2h"


def test_personal_and_service_keep_catalog_limit():
    for acc in (3, 4):
        token = _token(acc)
        assert effective_wb_rate_limit("wb_stats_orders", "1 req/min", token) == "1 req/min"
        assert effective_wb_rate_limit("wb_stats_sales", "1 req/min", token) == "1 req/min"


def test_unrelated_wb_operations_are_not_rewritten():
    assert effective_wb_rate_limit("wb_content_cards_list", "100 req/min", _token(1)) == "100 req/min"
    assert effective_wb_rate_limit("wb_stats_orders", "1 req/min", "malformed") == "1 req/min"
