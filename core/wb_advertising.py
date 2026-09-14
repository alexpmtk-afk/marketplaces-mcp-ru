"""Read-only Wildberries advertising business tools (M0).

M0 deliberately exposes only live campaign discovery and advertising-attribution
analytics. Mutating Promotion API operations remain available only through the
existing safety-gated generic layer until the dedicated control contract is
reviewed and accepted.
"""
from __future__ import annotations

import base64
import json
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from .business_registry import resolve_business_cabinet
from .errors import make_error
from .tools import resolve_named_cabinet

ADS_SERVICE = "wb_ads"
ADS_TOKEN_ENV = "WB_ADS_API_TOKEN"
PROMOTION_SCOPE_BIT = 1 << 5
ACTIVE_STATUS = 9
MAX_CAMPAIGNS_PER_STATS_CALL = 50
MAX_STATS_DAYS = 31
DEFAULT_AUDIT_DAYS = 7
METRIC_CONTRACT_VERSION = "wb_ads_m0.v1"


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _as_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (ValueError, TypeError):
        return 0


def _ratio_pct(numerator: Decimal | int, denominator: Decimal | int) -> float | None:
    den = Decimal(str(denominator))
    if den <= 0:
        return None
    num = Decimal(str(numerator))
    return float((num / den * Decimal("100")).quantize(Decimal("0.0001")))


def _ratio(numerator: Decimal | int, denominator: Decimal | int) -> float | None:
    den = Decimal(str(denominator))
    if den <= 0:
        return None
    num = Decimal(str(numerator))
    return float((num / den).quantize(Decimal("0.0001")))


def _token_has_promotion_scope(token: str) -> bool:
    """Prove WB Promotion category locally from the JWT ``s`` bitmask.

    This is intentionally fail-closed. It is used only when a normal named WB
    credential is reused as the backing secret for the logical ``wb_ads``
    service. An explicitly configured ``wb_ads`` credential remains authoritative.
    """
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
        scope_mask = int(claims.get("s") or 0)
        return bool(scope_mask & PROMOTION_SCOPE_BIT)
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _resolve_ads_creds(
    wb: Any,
    seller: str,
) -> tuple[dict[str, str] | None, dict[str, Any] | None, dict[str, str]]:
    """Resolve one named Promotion credential without changing shared state.

    Resolution order:
    1. Explicit ``wb_ads`` credential for the cabinet.
    2. The same named ``wb`` cabinet credential, but only when its JWT proves
       Promotion scope locally. This avoids duplicating an identical secret in
       Lockbox while keeping ``wb_ads`` as the logical service boundary.
    """
    business = resolve_business_cabinet("wb", seller)
    cabinet = business.cabinet if business else seller.strip()
    meta = {
        "seller": seller,
        "cabinet": cabinet,
        "business_entity": business.business_entity if business else seller,
        "credential_service": ADS_SERVICE,
    }
    if not cabinet:
        return None, make_error(
            "invalid_params",
            "seller must name a configured WB cabinet.",
            operation_id="wb_ads_credentials",
            retryable=False,
        ), meta

    explicit, resolved = wb.client.config.store.resolve_named(
        ADS_SERVICE,
        ["token"],
        {"token": ADS_TOKEN_ENV},
        cabinet,
    )
    if resolved and explicit.get("token"):
        meta.update({
            "credential_backing_service": ADS_SERVICE,
            "credential_binding": "explicit",
            "promotion_scope_proof": "dedicated_ads_credential",
        })
        return {"token": str(explicit["token"])}, None, meta

    wb_creds, wb_error = resolve_named_cabinet(wb.client, cabinet)
    if wb_error or not wb_creds or not wb_creds.get("token"):
        return None, {
            "ok": False,
            "error": "seller_known_but_ads_not_configured" if business else "ads_cabinet_not_configured",
            "code": "WB_ADS_CABINET_NOT_CONFIGURED",
            **meta,
            "credentials_status": "not_configured",
            "required_scope": "promotion",
            "upstream_request_sent": False,
            "retryable": False,
            "message": (
                "Для этого WB-кабинета нет доступного Promotion credential; "
                "запрос к Wildberries не отправлялся."
            ),
        }, meta

    token = str(wb_creds["token"])
    if not _token_has_promotion_scope(token):
        return None, {
            "ok": False,
            "error": "promotion_scope_unproven",
            "code": "WB_ADS_PROMOTION_SCOPE_UNPROVEN",
            **meta,
            "credentials_status": "scope_unproven",
            "credential_backing_service": "wb",
            "credential_binding": "scope_verified_alias",
            "required_scope": "promotion",
            "upstream_request_sent": False,
            "retryable": False,
            "message": (
                "WB-токен кабинета найден, но Promotion-доступ локально не подтверждён; "
                "запрос к Wildberries не отправлялся."
            ),
        }, meta

    meta.update({
        "credential_backing_service": "wb",
        "credential_binding": "scope_verified_alias",
        "promotion_scope_proof": "jwt_s_bit_6",
    })
    return {"token": token}, None, meta


def _campaign_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("adverts", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
            if isinstance(value, dict):
                nested = value.get("adverts") or value.get("items")
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]
    return []


def _stats_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("adverts", "stats", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _campaign_id(row: dict[str, Any]) -> int | None:
    for key in ("advertId", "advert_id", "id"):
        value = row.get(key)
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _normalize_stat(row: dict[str, Any]) -> dict[str, Any]:
    campaign_id = _campaign_id(row)
    spend = _as_decimal(row.get("sum"))
    order_amount = _as_decimal(row.get("sum_price"))
    views = _as_int(row.get("views"))
    clicks = _as_int(row.get("clicks"))
    atbs = _as_int(row.get("atbs"))
    orders = _as_int(row.get("orders"))
    canceled = _as_int(row.get("canceled"))
    shks = _as_int(row.get("shks"))
    return {
        "campaign_id": campaign_id,
        "views": views,
        "clicks": clicks,
        "cart_adds": atbs,
        "ad_orders": orders,
        "advertised_items": shks,
        "canceled": canceled,
        "spend": float(spend),
        "attributed_order_amount": float(order_amount),
        "ctr_pct": _ratio_pct(clicks, views),
        "cpc": _ratio(spend, clicks),
        "click_to_order_cr_pct": _ratio_pct(orders, clicks),
        "cpo": _ratio(spend, orders),
        "drr_order_pct": _ratio_pct(spend, order_amount),
        "roas": _ratio(order_amount, spend),
        "provider_metrics": {
            key: row.get(key) for key in ("ctr", "cpc", "cr") if row.get(key) is not None
        },
    }


def _totals(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(rows)
    spend = sum((_as_decimal(row.get("spend")) for row in rows), Decimal("0"))
    order_amount = sum((_as_decimal(row.get("attributed_order_amount")) for row in rows), Decimal("0"))
    views = sum(_as_int(row.get("views")) for row in rows)
    clicks = sum(_as_int(row.get("clicks")) for row in rows)
    atbs = sum(_as_int(row.get("cart_adds")) for row in rows)
    orders = sum(_as_int(row.get("ad_orders")) for row in rows)
    return {
        "views": views,
        "clicks": clicks,
        "cart_adds": atbs,
        "ad_orders": orders,
        "spend": float(spend),
        "attributed_order_amount": float(order_amount),
        "ctr_pct": _ratio_pct(clicks, views),
        "cpc": _ratio(spend, clicks),
        "click_to_order_cr_pct": _ratio_pct(orders, clicks),
        "cpo": _ratio(spend, orders),
        "drr_order_pct": _ratio_pct(spend, order_amount),
        "roas": _ratio(order_amount, spend),
    }


def _validate_period(date_from: str, date_to: str) -> tuple[date, date]:
    start = date.fromisoformat(str(date_from)[:10])
    end = date.fromisoformat(str(date_to)[:10])
    if start > end:
        raise ValueError("date_from must be <= date_to")
    if (end - start).days + 1 > MAX_STATS_DAYS:
        raise ValueError(f"WB advertising fullstats accepts at most {MAX_STATS_DAYS} calendar days")
    return start, end


def _credential_provenance(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "credential_service": ADS_SERVICE,
        "credential_backing_service": meta.get("credential_backing_service"),
        "credential_binding": meta.get("credential_binding"),
        "promotion_scope_proof": meta.get("promotion_scope_proof"),
    }


async def _list_active(wb: Any, seller: str, payment_type: str = "") -> dict[str, Any]:
    creds, error, meta = _resolve_ads_creds(wb, seller)
    if error:
        return error
    spec = wb.catalog.get("wb_get_api_advert_adverts")
    if spec is None:
        return make_error(
            "contract",
            "Current WB campaign-list contract is missing.",
            operation_id="wb_get_api_advert_adverts",
            retryable=False,
        )
    query: dict[str, Any] = {"statuses": str(ACTIVE_STATUS)}
    normalized_payment = str(payment_type or "").strip().lower()
    if normalized_payment:
        if normalized_payment not in {"cpm", "cpc"}:
            return make_error(
                "invalid_params",
                "payment_type must be 'cpm' or 'cpc'.",
                operation_id=spec.operation_id,
                retryable=False,
            )
        query["payment_type"] = normalized_payment
    response = await wb.client.call_spec(spec, query=query, creds_override=creds)
    if not response.get("ok"):
        return {**response, **meta}
    campaigns = _campaign_rows(response.get("data"))
    return {
        "ok": True,
        **meta,
        "live": True,
        "status_filter": ACTIVE_STATUS,
        "campaign_count": len(campaigns),
        "campaigns": campaigns,
        "source": spec.operation_id,
        "provenance": {
            "marketplace": "wb",
            **_credential_provenance(meta),
            "operation_id": spec.operation_id,
            "data_class": "live_campaign_state",
        },
    }


async def _get_stats(
    wb: Any,
    seller: str,
    campaign_ids: list[int],
    date_from: str,
    date_to: str,
) -> dict[str, Any]:
    try:
        start, end = _validate_period(date_from, date_to)
    except ValueError as exc:
        return make_error(
            "invalid_params",
            str(exc),
            operation_id="wb_get_adv_fullstats",
            retryable=False,
        )
    ids = sorted({int(value) for value in campaign_ids if int(value) > 0})
    if not ids:
        return make_error(
            "invalid_params",
            "campaign_ids must contain at least one positive ID.",
            operation_id="wb_get_adv_fullstats",
            retryable=False,
        )
    if len(ids) > MAX_CAMPAIGNS_PER_STATS_CALL:
        return {
            "ok": False,
            "error": "campaign_batch_too_large",
            "code": "WB_ADS_M0_MAX_50_CAMPAIGNS",
            "campaign_count": len(ids),
            "max_campaigns": MAX_CAMPAIGNS_PER_STATS_CALL,
            "retryable": False,
            "message": (
                "M0 refuses partial advertising analytics; split/batch scheduling "
                "belongs to the durable archive stage."
            ),
        }
    creds, error, meta = _resolve_ads_creds(wb, seller)
    if error:
        return error
    spec = wb.catalog.get("wb_get_adv_fullstats")
    if spec is None:
        return make_error(
            "contract",
            "Current WB /adv/v3/fullstats contract is missing.",
            operation_id="wb_get_adv_fullstats",
            retryable=False,
        )
    response = await wb.client.call_spec(
        spec,
        query={
            "ids": ",".join(str(value) for value in ids),
            "beginDate": start.isoformat(),
            "endDate": end.isoformat(),
        },
        creds_override=creds,
    )
    if not response.get("ok"):
        return {**response, **meta}
    raw_rows = _stats_rows(response.get("data"))
    normalized = [_normalize_stat(row) for row in raw_rows]
    returned_ids = {row["campaign_id"] for row in normalized if row.get("campaign_id")}
    missing = [value for value in ids if value not in returned_ids]
    return {
        "ok": True,
        **meta,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "requested_campaign_ids": ids,
        "returned_campaign_count": len(normalized),
        "missing_campaign_ids": missing,
        "coverage": "complete" if not missing else "partial_provider_response",
        "stats": normalized,
        "source": spec.operation_id,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "provenance": {
            "marketplace": "wb",
            **_credential_provenance(meta),
            "operation_id": spec.operation_id,
            "data_class": "advertising_attribution_operational",
            "period_days": (end - start).days + 1,
        },
    }


def register_wb_advertising_tools(mcp: FastMCP, modules: dict[str, Any]) -> None:
    """Register M0 read-only WB advertising tools on the combined MCP server."""
    wb = modules["wb"]

    @mcp.tool(
        name="wb_ads_list_active_campaigns",
        annotations={
            "title": "WB active advertising campaigns",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def wb_ads_list_active_campaigns(seller: str, payment_type: str = "") -> str:
        """Return live active (status=9) WB advertising campaigns for one named cabinet."""
        return _j(await _list_active(wb, seller, payment_type))

    @mcp.tool(
        name="wb_ads_get_campaign_stats",
        annotations={
            "title": "WB advertising campaign statistics",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def wb_ads_get_campaign_stats(
        seller: str,
        campaign_ids: list[int],
        date_from: str,
        date_to: str,
    ) -> str:
        """Return normalized WB ad-attribution statistics for <=50 campaigns and <=31 days."""
        return _j(await _get_stats(wb, seller, campaign_ids, date_from, date_to))

    @mcp.tool(
        name="wb_ads_audit_active",
        annotations={
            "title": "WB active advertising efficiency audit",
            "readOnlyHint": True,
            "openWorldHint": True,
        },
    )
    async def wb_ads_audit_active(seller: str, days: int = DEFAULT_AUDIT_DAYS) -> str:
        """Audit all currently active campaigns using the last N full MSK calendar days.

        M0 evaluates WB advertising attribution only. It does NOT claim actual
        business profit because real sales/buyouts, returns and unit economics are
        not joined yet. ``days`` must be 1..31 and defaults to 7.
        """
        days = int(days)
        if not 1 <= days <= MAX_STATS_DAYS:
            return _j(make_error(
                "invalid_params",
                f"days must be in 1..{MAX_STATS_DAYS}.",
                operation_id="wb_ads_audit_active",
                retryable=False,
            ))
        active = await _list_active(wb, seller)
        if not active.get("ok"):
            return _j(active)
        campaigns = active.get("campaigns", [])
        ids = [value for row in campaigns if (value := _campaign_id(row)) is not None]
        if len(ids) != len(campaigns):
            return _j({
                "ok": False,
                "error": "campaign_schema_unproven",
                "code": "WB_ADS_CAMPAIGN_ID_MISSING",
                "retryable": False,
                "message": (
                    "At least one active campaign has no recognized campaign ID; "
                    "audit stopped instead of returning partial results."
                ),
                "campaign_count": len(campaigns),
                "resolved_ids": ids,
            })
        if not ids:
            return _j({
                "ok": True,
                "seller": active.get("seller"),
                "cabinet": active.get("cabinet"),
                "business_entity": active.get("business_entity"),
                "active_campaign_count": 0,
                "campaigns": [],
                "evaluation_class": "advertising_attribution_operational",
                "business_profitability": "not_evaluated",
                "message": "Активных рекламных кампаний сейчас нет.",
                "provenance": [active.get("provenance")],
            })
        if len(ids) > MAX_CAMPAIGNS_PER_STATS_CALL:
            return _j({
                "ok": False,
                "error": "active_campaign_count_exceeds_m0_batch",
                "code": "WB_ADS_M0_MAX_50_CAMPAIGNS",
                "active_campaign_count": len(ids),
                "max_campaigns": MAX_CAMPAIGNS_PER_STATS_CALL,
                "retryable": False,
                "message": (
                    "M0 does not return a partial audit; durable rate-aware batching "
                    "will be added with Advertising Archive V1."
                ),
            })

        msk_today = datetime.now(ZoneInfo("Europe/Moscow")).date()
        end = msk_today - timedelta(days=1)
        start = end - timedelta(days=days - 1)
        stats = await _get_stats(wb, seller, ids, start.isoformat(), end.isoformat())
        if not stats.get("ok"):
            return _j(stats)

        metrics = stats.get("stats", [])
        campaign_meta = {_campaign_id(row): row for row in campaigns}
        evaluated = []
        for row in metrics:
            campaign_id = row.get("campaign_id")
            evaluated.append({
                **row,
                "campaign": campaign_meta.get(campaign_id, {}),
            })
        missing = stats.get("missing_campaign_ids", [])
        return _j({
            "ok": True,
            "seller": active.get("seller"),
            "cabinet": active.get("cabinet"),
            "business_entity": active.get("business_entity"),
            "active_campaign_count": len(ids),
            "period": {
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
                "timezone": "Europe/Moscow",
            },
            "evaluation_class": "advertising_attribution_operational",
            "business_profitability": "not_evaluated",
            "business_profitability_reason": (
                "M0 does not yet join real sales/buyouts, returns, finance and unit economics."
            ),
            "coverage": "complete" if not missing else "partial_provider_response",
            "missing_campaign_ids": missing,
            "portfolio": _totals(evaluated),
            "campaigns": evaluated,
            "metric_contract_version": METRIC_CONTRACT_VERSION,
            "quality": {
                "ad_orders_are_provider_attributed": True,
                "attributed_order_amount_is_not_actual_sales": True,
                "no_silent_zero_for_missing_campaign_stats": True,
            },
            "provenance": [active.get("provenance"), stats.get("provenance")],
        })
