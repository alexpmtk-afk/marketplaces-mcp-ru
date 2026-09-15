"""Coverage-gated Semantic Core execution for canonical WB advertising history."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .archive_coverage import parse_registry
from .business_registry import resolve_business_cabinet
from .wb_advertising import METRIC_CONTRACT_VERSION, _totals
from .wb_advertising_archive import ARCHIVE_CABINETS, canonical_location, parse_csv

CAMPAIGN_DATASET = "ads_campaign_daily"
ROSTER_DATASET = "ads_campaign_roster_snapshots"
COVERAGE_FOLDER = ["app", "registry"]
COVERAGE_FILE = "dataset_coverage_registry.csv"
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
_PRODUCT_WORDS = re.compile(r"\b(товар|товару|товара|артикул|карточк|sku|nm\s*id|nmid)\b", re.IGNORECASE)
_CAMPAIGN_SELECTOR = re.compile(
    r"(?:\b(?:кампан\w*|campaign)\s*(?:id|№|#)?\s*\d+\b|"
    r"\b(?:по\s+кампаниям|разбивк\w*\s+по\s+кампаниям|кажд\w*\s+кампан\w*)\b)",
    re.IGNORECASE,
)


class SemanticAdvertisingExecutionError(RuntimeError):
    """Raised when advertising semantics or archive coverage are not safe to execute."""


def _parse_day(value: str | date, field: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError) as exc:
        raise SemanticAdvertisingExecutionError(
            f"{field} must be an ISO calendar date (YYYY-MM-DD)"
        ) from exc


def _moscow_today() -> date:
    return datetime.now(MOSCOW_TZ).date()


def _resolve_cabinet(seller: str) -> str:
    value = str(seller).strip()
    business = resolve_business_cabinet("wb", value)
    cabinet = business.cabinet if business else value
    if cabinet not in ARCHIVE_CABINETS:
        raise SemanticAdvertisingExecutionError(
            f"Seller {seller!r} is not a supported canonical WB advertising cabinet"
        )
    return cabinet


def _bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _registry_scope(row: dict[str, str]) -> dict[str, Any]:
    try:
        value = json.loads(str(row.get("scope_json") or "{}"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _quality_allowed(row: dict[str, str]) -> bool:
    return (
        row.get("marketplace") == "wb"
        and row.get("status") == "COMPLETE"
        and row.get("quality_status") in {"PASS", "PASS_WITH_FLAGS"}
    )


def _record_interval(row: dict[str, str]) -> tuple[date, date] | None:
    try:
        start = date.fromisoformat(str(row.get("date_from") or "")[:10])
        end = date.fromisoformat(str(row.get("date_to") or "")[:10])
    except ValueError:
        return None
    if start > end:
        return None
    return start, end


def _merged_intervals(
    rows: list[dict[str, str]],
    *,
    cabinet: str,
    dataset: str,
    campaign_id: int | None = None,
) -> list[tuple[date, date]]:
    intervals: list[tuple[date, date]] = []
    for row in rows:
        if not _quality_allowed(row):
            continue
        if row.get("cabinet") != cabinet or row.get("dataset") != dataset:
            continue
        if campaign_id is not None:
            scope = _registry_scope(row)
            raw_ids = scope.get("campaign_ids")
            if not isinstance(raw_ids, list):
                continue
            try:
                campaign_ids = {int(value) for value in raw_ids}
            except (TypeError, ValueError):
                continue
            if campaign_id not in campaign_ids:
                continue
        interval = _record_interval(row)
        if interval is not None:
            intervals.append(interval)

    intervals.sort()
    merged: list[list[date]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + timedelta(days=1):
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return [(item[0], item[1]) for item in merged]


def _gaps(intervals: list[tuple[date, date]], start: date, end: date) -> list[list[str]]:
    cursor = start
    gaps: list[list[str]] = []
    for interval_start, interval_end in intervals:
        if interval_end < start or interval_start > end:
            continue
        clipped_start = max(start, interval_start)
        clipped_end = min(end, interval_end)
        if clipped_start > cursor:
            gaps.append([cursor.isoformat(), (clipped_start - timedelta(days=1)).isoformat()])
        cursor = max(cursor, clipped_end + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        gaps.append([cursor.isoformat(), end.isoformat()])
    return gaps


def evaluate_advertising_coverage(
    registry_data: bytes | None,
    *,
    cabinet: str,
    date_from: str | date,
    date_to: str | date,
    expected_campaign_ids: set[int],
) -> dict[str, Any]:
    """Prove roster and fullstats coverage for every expected campaign."""
    start = _parse_day(date_from, "date_from")
    end = _parse_day(date_to, "date_to")
    rows = parse_registry(registry_data)

    roster_intervals = _merged_intervals(
        rows, cabinet=cabinet, dataset=ROSTER_DATASET,
    )
    roster_gaps = _gaps(roster_intervals, start, end)
    campaign_gaps: dict[str, list[list[str]]] = {}
    for campaign_id in sorted(expected_campaign_ids):
        intervals = _merged_intervals(
            rows,
            cabinet=cabinet,
            dataset=CAMPAIGN_DATASET,
            campaign_id=campaign_id,
        )
        missing = _gaps(intervals, start, end)
        if missing:
            campaign_gaps[str(campaign_id)] = missing

    status = "FULL_COVERAGE" if not roster_gaps and not campaign_gaps else "PARTIAL_COVERAGE"
    if not roster_intervals:
        status = "NO_COVERAGE"
    return {
        "status": status,
        "cabinet": cabinet,
        "dataset": CAMPAIGN_DATASET,
        "requested_from": start.isoformat(),
        "requested_to": end.isoformat(),
        "expected_campaign_ids": sorted(expected_campaign_ids),
        "expected_campaign_count": len(expected_campaign_ids),
        "roster_gaps": roster_gaps,
        "campaign_gaps": campaign_gaps,
        "safe_for_archive_only_query": status == "FULL_COVERAGE",
    }


async def _download_dataset_year(store: Any, cabinet: str, year: int, dataset: str) -> list[dict[str, str]]:
    parts, name = canonical_location(cabinet, year, dataset)
    parent = await store.ensure_folder_path(parts)
    item, raw = await store.download_named(parent, name)
    if item is None or raw is None:
        return []
    _, rows = parse_csv(raw)
    return rows


async def execute_semantic_advertising_question(
    store: Any,
    *,
    question: str,
    seller: str,
    date_from: str | date,
    date_to: str | date,
    nm_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Execute the approved cabinet-level historical advertising contract."""
    start = _parse_day(date_from, "date_from")
    end = _parse_day(date_to, "date_to")
    if start > end:
        raise SemanticAdvertisingExecutionError("date_from must be <= date_to")
    if end >= _moscow_today():
        raise SemanticAdvertisingExecutionError(
            "Advertising Archive contains only closed historical days; today/current live advertising must use the live advertising source."
        )
    if nm_ids:
        raise SemanticAdvertisingExecutionError(
            "Product-filtered advertising requires the separately registered ads_product_daily semantic contract; cabinet totals will not be substituted."
        )
    question_text = str(question or "")
    if _PRODUCT_WORDS.search(question_text):
        raise SemanticAdvertisingExecutionError(
            "This question is product-scoped, but advertising_performance V1 is cabinet-level only. ads_product_daily must be approved before execution."
        )
    if _CAMPAIGN_SELECTOR.search(question_text):
        raise SemanticAdvertisingExecutionError(
            "Campaign-filtered or campaign-breakdown advertising is not approved in advertising_performance V1; cabinet totals will not be substituted for a campaign-scoped question."
        )

    cabinet = _resolve_cabinet(seller)
    years = range(start.year, end.year + 1)
    roster_rows: list[dict[str, str]] = []
    campaign_rows: list[dict[str, str]] = []
    for year in years:
        roster_rows.extend(await _download_dataset_year(store, cabinet, year, ROSTER_DATASET))
        campaign_rows.extend(await _download_dataset_year(store, cabinet, year, CAMPAIGN_DATASET))

    expected_campaign_ids = {
        int(row.get("campaign_id") or 0)
        for row in roster_rows
        if _bool(row.get("fullstats_eligible")) and str(row.get("campaign_id") or "").isdigit()
    }
    expected_campaign_ids.discard(0)

    registry_parent = await store.ensure_folder_path(COVERAGE_FOLDER)
    _, registry_raw = await store.download_named(registry_parent, COVERAGE_FILE)
    coverage = evaluate_advertising_coverage(
        registry_raw,
        cabinet=cabinet,
        date_from=start,
        date_to=end,
        expected_campaign_ids=expected_campaign_ids,
    )
    if coverage["status"] != "FULL_COVERAGE":
        raise SemanticAdvertisingExecutionError(
            "Canonical WB advertising archive does not prove FULL_COVERAGE for the requested period and expected campaign set."
        )

    filtered: list[dict[str, Any]] = []
    for row in campaign_rows:
        try:
            row_day = date.fromisoformat(str(row.get("date") or "")[:10])
        except ValueError:
            continue
        if start <= row_day <= end:
            filtered.append(row)

    metrics = _totals(filtered)
    observed_campaign_ids = sorted({
        int(row.get("campaign_id") or 0)
        for row in filtered
        if str(row.get("campaign_id") or "").isdigit() and int(row.get("campaign_id") or 0) > 0
    })
    return {
        "ok": True,
        "metric": "ADVERTISING_PERFORMANCE",
        "marketplace": "WB",
        "seller": seller,
        "cabinet": cabinet,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "route": "semantic_advertising_archive",
        "source": CAMPAIGN_DATASET,
        "source_validation": "canonical_google_drive_archive_with_dataset_coverage_registry",
        "data_class": "ADVERTISING_ATTRIBUTION_OPERATIONAL",
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "aggregation_scope": "cabinet_total",
        "complete": True,
        "coverage": coverage,
        "campaign_ids_observed": observed_campaign_ids,
        "campaign_count_observed": len(observed_campaign_ids),
        "metrics": metrics,
        "business_profitability": "not_evaluated",
        "guardrail": (
            "DRR/ROAS and attributed order metrics are WB advertising-attribution metrics. "
            "They are not total seller revenue, complete marketplace orders or business profitability."
        ),
    }
