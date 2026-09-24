"""Normalization and bounded request planning for WB Advertising Archive V1."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

MAX_FULLSTATS_DAYS = 31
MAX_FULLSTATS_CAMPAIGNS = 50
MAX_CLUSTER_ITEMS = 100


def _date(value: Any) -> str:
    text = str(value or "")
    if len(text) < 10:
        raise ValueError(f"invalid date value: {value!r}")
    return date.fromisoformat(text[:10]).isoformat()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _money(value: Any) -> str:
    try:
        decimal = Decimal(str(value or 0))
    except (InvalidOperation, TypeError, ValueError):
        decimal = Decimal("0")
    return format(decimal, "f")


def _nullable_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def split_date_range(date_from: str, date_to: str, *, max_days: int = MAX_FULLSTATS_DAYS) -> list[tuple[str, str]]:
    """Split an inclusive date interval into deterministic provider-sized chunks."""
    start = date.fromisoformat(date_from[:10])
    end = date.fromisoformat(date_to[:10])
    if start > end:
        raise ValueError("date_from must be <= date_to")
    if max_days < 1:
        raise ValueError("max_days must be positive")
    chunks: list[tuple[str, str]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=max_days - 1))
        chunks.append((cursor.isoformat(), chunk_end.isoformat()))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def chunk_ids(values: Iterable[int], *, size: int) -> list[list[int]]:
    unique = sorted({int(value) for value in values if int(value) > 0})
    if size < 1:
        raise ValueError("size must be positive")
    return [unique[index:index + size] for index in range(0, len(unique), size)]


def plan_fullstats_requests(campaign_ids: Iterable[int], date_from: str, date_to: str) -> list[dict[str, Any]]:
    """Plan complete /adv/v3/fullstats coverage without exceeding WB limits."""
    id_chunks = chunk_ids(campaign_ids, size=MAX_FULLSTATS_CAMPAIGNS)
    if not id_chunks:
        return []
    periods = split_date_range(date_from, date_to, max_days=MAX_FULLSTATS_DAYS)
    return [
        {
            "operation_id": "wb_get_adv_fullstats",
            "campaign_ids": ids,
            "date_from": start,
            "date_to": end,
        }
        for start, end in periods
        for ids in id_chunks
    ]


def plan_period_requests(operation_id: str, date_from: str, date_to: str) -> list[dict[str, str]]:
    """Plan 31-day bounded requests for Promotion finance-history endpoints."""
    return [
        {"operation_id": operation_id, "date_from": start, "date_to": end}
        for start, end in split_date_range(date_from, date_to, max_days=31)
    ]


def normalize_fullstats(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize WB fullstats into campaign/day and product/day/app grains.

    Product-role classification is based on the historical fullstats payload
    itself, not on the campaign's current settings. This avoids projecting
    today's campaign composition backwards into older periods.

    A product is classified as:
    - ad_traffic_product: it has own ad views/clicks/spend somewhere in this
      provider payload window;
    - associated_conversion_candidate: it has conversion activity but no own
      ad traffic/spend in the payload window;
    - no_activity: neither traffic nor conversion activity is present.

    Missing campaign rows are intentionally not created here. Coverage
    validation belongs to the caller/registry and must fail closed instead of
    manufacturing zero-valued rows.
    """
    campaigns = payload if isinstance(payload, list) else []
    campaign_daily: list[dict[str, Any]] = []
    product_daily: list[dict[str, Any]] = []

    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        campaign_id = _int(campaign.get("advertId"))
        if campaign_id <= 0:
            continue

        booster_by_day_nm: dict[tuple[str, int], float | None] = {}
        for booster in campaign.get("boosterStats") or []:
            if not isinstance(booster, dict):
                continue
            nm_id = _int(booster.get("nm"))
            if nm_id <= 0:
                continue
            try:
                day_value = _date(booster.get("date"))
            except (TypeError, ValueError):
                continue
            booster_by_day_nm[(day_value, nm_id)] = _nullable_number(
                booster.get("avg_position")
                if booster.get("avg_position") is not None
                else booster.get("avgPosition")
            )

        traffic_nm_ids: set[int] = set()
        for day in campaign.get("days") or []:
            if not isinstance(day, dict):
                continue
            for app in day.get("apps") or []:
                if not isinstance(app, dict):
                    continue
                for nm in app.get("nms") or []:
                    if not isinstance(nm, dict):
                        continue
                    nm_id = _int(nm.get("nmId"))
                    if nm_id <= 0:
                        continue
                    own_spend = _nullable_number(nm.get("sum")) or 0.0
                    if _int(nm.get("views")) > 0 or _int(nm.get("clicks")) > 0 or own_spend > 0:
                        traffic_nm_ids.add(nm_id)

        for day in campaign.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_value = _date(day.get("date"))
            campaign_daily.append({
                "date": day_value,
                "campaign_id": campaign_id,
                "views": _int(day.get("views")),
                "clicks": _int(day.get("clicks")),
                "cart_adds": _int(day.get("atbs")),
                "ad_orders": _int(day.get("orders")),
                "advertised_items": _int(day.get("shks")),
                "canceled": _int(day.get("canceled")),
                "spend": _money(day.get("sum")),
                "attributed_order_amount": _money(day.get("sum_price")),
            })
            for app in day.get("apps") or []:
                if not isinstance(app, dict):
                    continue
                app_type = _int(app.get("appType"))
                for nm in app.get("nms") or []:
                    if not isinstance(nm, dict):
                        continue
                    nm_id = _int(nm.get("nmId"))
                    if nm_id <= 0:
                        continue
                    has_conversion = (
                        _int(nm.get("atbs")) > 0
                        or _int(nm.get("orders")) > 0
                        or _int(nm.get("shks")) > 0
                        or (_nullable_number(nm.get("sum_price")) or 0.0) > 0
                    )
                    if nm_id in traffic_nm_ids:
                        product_role = "ad_traffic_product"
                    elif has_conversion:
                        product_role = "associated_conversion_candidate"
                    else:
                        product_role = "no_activity"
                    product_daily.append({
                        "date": day_value,
                        "campaign_id": campaign_id,
                        "app_type": app_type,
                        "nm_id": nm_id,
                        "product_role": product_role,
                        "name": nm.get("name"),
                        "views": _int(nm.get("views")),
                        "clicks": _int(nm.get("clicks")),
                        "cart_adds": _int(nm.get("atbs")),
                        "ad_orders": _int(nm.get("orders")),
                        "advertised_items": _int(nm.get("shks")),
                        "canceled": _int(nm.get("canceled")),
                        "spend": _money(nm.get("sum")),
                        "attributed_order_amount": _money(nm.get("sum_price")),
                        "avg_position": booster_by_day_nm.get((day_value, nm_id)),
                    })
    return {"ads_campaign_daily": campaign_daily, "ads_product_daily": product_daily}


def normalize_search_cluster_daily(
    payload: Any,
    *,
    payment_type_by_campaign: Mapping[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Normalize /adv/v1/normquery/stats, preserving CPC metric unavailability."""
    mapping = {int(key): str(value).lower() for key, value in (payment_type_by_campaign or {}).items()}
    root = payload if isinstance(payload, dict) else {}
    output: list[dict[str, Any]] = []
    for item in root.get("items") or []:
        if not isinstance(item, dict):
            continue
        campaign_id = _int(item.get("advertId"))
        nm_id = _int(item.get("nmId"))
        payment_type = mapping.get(campaign_id)
        for daily in item.get("dailyStats") or []:
            if not isinstance(daily, dict):
                continue
            stat = daily.get("stat") if isinstance(daily.get("stat"), dict) else {}
            cpc = payment_type == "cpc"
            output.append({
                "date": _date(daily.get("date")),
                "campaign_id": campaign_id,
                "nm_id": nm_id,
                "norm_query": stat.get("normQuery"),
                "payment_type": payment_type,
                "views": None if cpc else _int(stat.get("views")),
                "clicks": _int(stat.get("clicks")),
                "cart_adds": _int(stat.get("atbs")),
                "orders": _int(stat.get("orders")),
                "ordered_items": _int(stat.get("shks")),
                "spend": _money(stat.get("spend")),
                "avg_position": _nullable_number(stat.get("avgPos")),
                "ctr": None if cpc else _nullable_number(stat.get("ctr")),
                "cpc": _nullable_number(stat.get("cpc")),
                "cpm": None if cpc else _nullable_number(stat.get("cpm")),
                "quality_flags": ["cpc_views_ctr_cpm_not_available"] if cpc else [],
            })
    return output
