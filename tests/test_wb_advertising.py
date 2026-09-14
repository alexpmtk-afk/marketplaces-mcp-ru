from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp import FastMCP

from core.registry import Catalog
from core.wb_advertising import (
    _get_stats,
    _list_active,
    _normalize_stat,
    _token_has_promotion_scope,
    _validate_period,
    register_wb_advertising_tools,
)

ROOT = Path(__file__).resolve().parent.parent


def _jwt(scope_mask: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps({"s": scope_mask}, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{payload}.signature"


class FakeStore:
    def __init__(self, *, explicit_ads: bool = True, wb_scope_mask: int = 0):
        self.explicit_ads = explicit_ads
        self.wb_scope_mask = wb_scope_mask

    def resolve_named(self, service, fields, env_map, name):
        del fields, env_map
        if service == "wb_ads" and name == "wb_novokshenov" and self.explicit_ads:
            return {"token": "header.payload.signature"}, name
        if service == "wb" and name == "wb_novokshenov":
            return {"token": _jwt(self.wb_scope_mask)}, name
        return {}, ""


class FakeCatalog:
    def get(self, operation_id):
        return SimpleNamespace(operation_id=operation_id)


class FakeClient:
    def __init__(self, *, explicit_ads: bool = True, wb_scope_mask: int = 0):
        self.config = SimpleNamespace(
            name="wb",
            fields=["token"],
            env_map={"token": "WB_API_TOKEN"},
            store=FakeStore(explicit_ads=explicit_ads, wb_scope_mask=wb_scope_mask),
        )
        self.calls = []

    async def call_spec(self, spec, **kwargs):
        self.calls.append((spec.operation_id, kwargs))
        if spec.operation_id == "wb_get_api_advert_adverts":
            return {
                "ok": True,
                "data": {"adverts": [{"advertId": 101, "status": 9}]},
            }
        if spec.operation_id == "wb_get_adv_fullstats":
            return {
                "ok": True,
                "data": [{
                    "advertId": 101,
                    "views": 1000,
                    "clicks": 100,
                    "atbs": 20,
                    "orders": 10,
                    "shks": 10,
                    "canceled": 1,
                    "sum": 500,
                    "sum_price": 5000,
                }],
            }
        raise AssertionError(spec.operation_id)


class FakeWB:
    def __init__(self, *, explicit_ads: bool = True, wb_scope_mask: int = 0):
        self.client = FakeClient(explicit_ads=explicit_ads, wb_scope_mask=wb_scope_mask)
        self.catalog = FakeCatalog()


def test_normalized_ad_metrics_are_explicit():
    row = _normalize_stat({
        "advertId": 101,
        "views": 1000,
        "clicks": 100,
        "atbs": 20,
        "orders": 10,
        "sum": 500,
        "sum_price": 5000,
    })
    assert row["campaign_id"] == 101
    assert row["ctr_pct"] == 10.0
    assert row["cpc"] == 5.0
    assert row["click_to_order_cr_pct"] == 10.0
    assert row["cpo"] == 50.0
    assert row["drr_order_pct"] == 10.0
    assert row["roas"] == 10.0


def test_stats_period_is_capped_at_31_days():
    _validate_period("2026-08-01", "2026-08-31")
    try:
        _validate_period("2026-08-01", "2026-09-01")
    except ValueError as exc:
        assert "31" in str(exc)
    else:
        raise AssertionError("32-day period must fail")


def test_promotion_scope_decoder_is_fail_closed():
    assert _token_has_promotion_scope(_jwt(1 << 5)) is True
    assert _token_has_promotion_scope(_jwt(0)) is False
    assert _token_has_promotion_scope("not-a-jwt") is False
    assert _token_has_promotion_scope("a.@@@.c") is False


def test_explicit_ads_credential_remains_authoritative():
    wb = FakeWB(explicit_ads=True, wb_scope_mask=0)
    result = asyncio.run(_list_active(wb, "Новокшенов"))
    assert result["ok"] is True
    assert result["credential_service"] == "wb_ads"
    assert result["credential_backing_service"] == "wb_ads"
    assert result["credential_binding"] == "explicit"
    assert result["provenance"]["credential_backing_service"] == "wb_ads"
    assert len(wb.client.calls) == 1


def test_active_campaigns_can_use_scope_verified_wb_alias():
    wb = FakeWB(explicit_ads=False, wb_scope_mask=1 << 5)
    result = asyncio.run(_list_active(wb, "Новокшенов"))
    assert result["ok"] is True
    assert result["cabinet"] == "wb_novokshenov"
    assert result["campaign_count"] == 1
    assert result["credential_service"] == "wb_ads"
    assert result["credential_backing_service"] == "wb"
    assert result["credential_binding"] == "scope_verified_alias"
    assert result["promotion_scope_proof"] == "jwt_s_bit_6"
    operation_id, kwargs = wb.client.calls[0]
    assert operation_id == "wb_get_api_advert_adverts"
    assert kwargs["query"] == {"statuses": "9"}
    assert kwargs["creds_override"]["token"]


def test_normal_wb_token_without_promotion_scope_fails_closed():
    wb = FakeWB(explicit_ads=False, wb_scope_mask=0)
    result = asyncio.run(_list_active(wb, "Новокшенов"))
    assert result["ok"] is False
    assert result["code"] == "WB_ADS_PROMOTION_SCOPE_UNPROVEN"
    assert result["upstream_request_sent"] is False
    assert wb.client.calls == []


def test_stats_refuse_more_than_one_m0_batch():
    wb = FakeWB()
    result = asyncio.run(_get_stats(
        wb,
        "Новокшенов",
        list(range(1, 52)),
        "2026-09-01",
        "2026-09-07",
    ))
    assert result["ok"] is False
    assert result["error"] == "campaign_batch_too_large"
    assert wb.client.calls == []


def test_stats_return_provenance_and_coverage():
    wb = FakeWB(explicit_ads=False, wb_scope_mask=1 << 5)
    result = asyncio.run(_get_stats(
        wb,
        "Новокшенов",
        [101],
        "2026-09-01",
        "2026-09-07",
    ))
    assert result["ok"] is True
    assert result["coverage"] == "complete"
    assert result["stats"][0]["drr_order_pct"] == 10.0
    assert result["provenance"]["data_class"] == "advertising_attribution_operational"
    assert result["provenance"]["credential_service"] == "wb_ads"
    assert result["provenance"]["credential_backing_service"] == "wb"
    assert result["provenance"]["credential_binding"] == "scope_verified_alias"


def test_m0_tools_are_registered_read_only():
    mcp = FastMCP("wb-ads-m0-test")
    register_wb_advertising_tools(mcp, {"wb": FakeWB()})
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert {
        "wb_ads_list_active_campaigns",
        "wb_ads_get_campaign_stats",
        "wb_ads_audit_active",
    } <= set(tools)
    for name in (
        "wb_ads_list_active_campaigns",
        "wb_ads_get_campaign_stats",
        "wb_ads_audit_active",
    ):
        assert tools[name].annotations.readOnlyHint is True


def test_runtime_promotion_overrides_fix_mutating_get_safety():
    catalog = Catalog.from_yaml(ROOT / "wb_mcp" / "endpoints.yaml")
    assert catalog.get("wb_adv_fullstats") is None
    assert catalog.get("wb_get_adv_fullstats").path == "/adv/v3/fullstats"
    assert catalog.get("wb_get_adv_fullstats").quota_proven is True
    assert catalog.get("wb_get_api_advert_adverts").quota_proven is True
    assert catalog.get("wb_get_adv_start").safety == "write"
    assert catalog.get("wb_get_adv_pause").safety == "write"
    assert catalog.get("wb_get_adv_stop").safety == "write"
    assert catalog.get("wb_get_adv_delete").safety == "destructive"
