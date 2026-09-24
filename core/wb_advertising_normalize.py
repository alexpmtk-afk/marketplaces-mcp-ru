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


def _accepted_orders(created_orders: Any, canceled: Any) -> int:
    """Derived accepted orders used only as an explicit reconciliation helper.

    WB fullstats exposes created advertising-attributed orders and technical
    cancellations separately. The seller XLS Evidence Gate matched
    max(orders - canceled, 0) to its "Принятые заказы" column. Keep the field
    explicitly derived so it is never confused with an independent provider
    measure.
    """
    return max(0, _int(created_orders) - _int(canceled))


def campaign_product_ids(payload: Any) -> dict[int, set[int]]:
    """Extract the current campaign -> product membership from /api/advert/v2/adverts."""
    if isinstance(payload, list):
        campaigns = payload
    elif isinstance(payload, dict):
        campaigns = payload.get("adverts") or payload.get("data") or []
        if isinstance(campaigns, dict):
            campaigns = campaigns.get("adverts") or []
    else:
        campaigns = []
    output: dict[int, set[int]] = {}
    for campaign in campaigns:
        if not isinstance(campaign, dict):
            continue
        campaign_id = _int(campaign.get("id") or campaign.get("advertId"))
        if campaign_id <= 0:
            continue
        ids: set[int] = set()
        for item in campaign.get("nm_settings") or campaign.get("nmSettings") or []:
            if isinstance(item, dict):
                nm_id = _int(item.get("nm_id") or item.get("nmId"))
                if nm_id > 0:
                    ids.add(nm_id)
        for value in campaign.get("nms") or []:
            nm_id = _int(value.get("nm_id") or value.get("nmId")) if isinstance(value, dict) else _int(value)
            if nm_id > 0:
                ids.add(nm_id)
        output[campaign_id] = ids
    return output


def normalize_product_identity_snapshot(payload: Any, *, observed_at: str) -> list[dict[str, Any]]:
    """Normalize Content API card identity needed for multicard attribution."""
    observed_at = str(observed_at).strip()
    if not observed_at:
        raise ValueError("observed_at is required")
    root = payload if isinstance(payload, dict) else {}
    rows = root.get("cards") or []
    output: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        nm_id = _int(raw.get("nmID") or raw.get("nmId"))
        if nm_id <= 0:
            continue
        output.append({
            "observed_at": observed_at,
            "nm_id": nm_id,
            "imt_id": _int(raw.get("imtID") or raw.get("imtId")) or None,
            "title": raw.get("title"),
            "vendor_code": raw.get("vendorCode"),
            "subject_id": _int(raw.get("subjectID") or raw.get("subjectId")) or None,
            "resolution_status": "resolved_current",
            "raw_json": __import__("json").dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        })
    output.sort(key=lambda row: int(row["nm_id"]))
    return output


def enrich_product_attribution(
    rows: Iterable[Mapping[str, Any]],
    *,
    advertised_nm_ids_by_campaign: Mapping[int, Iterable[int]],
    imt_id_by_nm: Mapping[int, int | None],
    observed_at: str,
) -> list[dict[str, Any]]:
    """Add current-snapshot conversion classification without rewriting event time.

    direct      = nm_id is currently configured in the campaign
    multicard   = another nm_id sharing the current imtID with a configured item
    associated  = another seller item outside the current multicard

    The classification is intentionally suffixed/current-scoped because product
    grouping and campaign membership can change after the historical event.
    """
    observed_at = str(observed_at).strip()
    if not observed_at:
        raise ValueError("observed_at is required")
    advertised = {
        int(campaign_id): {int(nm) for nm in values if int(nm) > 0}
        for campaign_id, values in advertised_nm_ids_by_campaign.items()
    }
    identities = {int(nm): (int(imt) if imt not in (None, "", 0, "0") else None) for nm, imt in imt_id_by_nm.items()}
    output: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        campaign_id = _int(row.get("campaign_id"))
        nm_id = _int(row.get("nm_id"))
        campaign_nms = advertised.get(campaign_id, set())
        advertised_imts = {identities.get(nm) for nm in campaign_nms if identities.get(nm)}
        imt_id = identities.get(nm_id)
        if nm_id in campaign_nms:
            conversion_type = "direct"
        elif not imt_id or not advertised_imts:
            conversion_type = "unknown"
        elif imt_id in advertised_imts:
            conversion_type = "multicard"
        elif nm_id > 0:
            conversion_type = "associated"
        else:
            conversion_type = "unknown"
        row.update({
            "multicard_id_current": imt_id,
            "conversion_type_current": conversion_type,
            "conversion_type_observed_at": observed_at,
            "conversion_type_quality_flags": ["current_snapshot_not_event_time"],
        })
        output.append(row)
    return output


def normalize_fullstats(payload: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize WB fullstats into campaign/day and product/day/app grains.

    Missing campaign rows are intentionally not created here. Coverage validation
    belongs to the caller/registry and must fail closed instead of manufacturing
    zero-valued rows.
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
        positions: dict[tuple[str, int], float | None] = {}
        for booster in campaign.get("boosterStats") or []:
            if not isinstance(booster, dict):
                continue
            nm_id = _int(booster.get("nm"))
            if nm_id <= 0:
                continue
            try:
                key_date = _date(booster.get("date"))
            except ValueError:
                continue
            positions[(key_date, nm_id)] = _nullable_number(booster.get("avg_position"))

        for day in campaign.get("days") or []:
            if not isinstance(day, dict):
                continue
            day_value = _date(day.get("date"))
            created_orders = _int(day.get("orders"))
            canceled = _int(day.get("canceled"))
            campaign_daily.append({
                "date": day_value,
                "campaign_id": campaign_id,
                "views": _int(day.get("views")),
                "clicks": _int(day.get("clicks")),
                "cart_adds": _int(day.get("atbs")),
                "ad_orders": created_orders,
                "accepted_orders_derived": _accepted_orders(created_orders, canceled),
                "advertised_items": _int(day.get("shks")),
                "canceled": canceled,
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
                    created_nm_orders = _int(nm.get("orders"))
                    canceled_nm = _int(nm.get("canceled"))
                    product_daily.append({
                        "date": day_value,
                        "campaign_id": campaign_id,
                        "app_type": app_type,
                        "nm_id": nm_id,
                        "name": nm.get("name"),
                        "views": _int(nm.get("views")),
                        "clicks": _int(nm.get("clicks")),
                        "cart_adds": _int(nm.get("atbs")),
                        "ad_orders": created_nm_orders,
                        "accepted_orders_derived": _accepted_orders(created_nm_orders, canceled_nm),
                        "advertised_items": _int(nm.get("shks")),
                        "canceled": canceled_nm,
                        "spend": _money(nm.get("sum")),
                        "attributed_order_amount": _money(nm.get("sum_price")),
                        "avg_position": positions.get((day_value, nm_id)),
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
